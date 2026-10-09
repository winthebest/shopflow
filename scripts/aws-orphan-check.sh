#!/usr/bin/env bash
# After a teardown nothing billed by the hour may remain, in any region: instances, available EBS
# volumes, load balancers, Elastic IPs, NAT gateways, available ENIs, EKS clusters. Layers 0 and 1
# own none of these, so every hit is an orphan.
#   shopflow-tagged orphans: deleted with --delete-tagged (in the home region; others are reported)
#   anything else:           reported, never touched; exit 2 so a human looks at it
# Load balancers count as shopflow's only with the `project` tag, the one the reapers' IAM conditions need. A
# load balancer in the shopflow VPC without it (for example a Classic ELB made by EKS's legacy cloud provider
# when a Service had no NLB class) is reported as such and blocks cloud-down until a human removes it.
# Exit codes: 0 clean, 2 unknown resources found, 3 shopflow resources remain, 1 error.
set -euo pipefail
# shellcheck source=scripts/cloud-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/cloud-lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/aws-orphan-check.sh [--dry-run] [--delete-tagged] [--alert] [--regions "r1 r2"]

  --delete-tagged  delete orphans tagged as shopflow (home region only)
  --alert          publish findings to the shopflow-alerts SNS topic
  --regions LIST   regions to scan (default: every region enabled in the account)
EOF
}

DELETE_TAGGED=0
ALERT=0
REGIONS=""

# A resource is "ours" when it carries the project tag or a tag the session's controllers set; load balancers
# only with the project tag (see above).
# shellcheck disable=SC2016 # a jq program, not shell
OURS_JQ='def project($p): any(.tags[]?; .Key == "project" and .Value == $p);
def ours($p; $c): if (.type | test("load-balancer$")) then project($p) else project($p) or any(.tags[]?;
  (.Key == "elbv2.k8s.aws/cluster" and .Value == $c)
  or (.Key == "cluster.k8s.amazonaws.com/name" and .Value == $c)
  or (.Key == ("kubernetes.io/cluster/" + $c))) end;'

in_region() {
  local region="$1"
  shift
  aws --region "$region" --output json "$@"
}

# One JSON object per resource: {region, type, id, tags}; load balancers add {in_shopflow_vpc}.
scan_region() {
  local r="$1" vpcs lbs
  in_region "$r" ec2 describe-instances --filters Name=instance-state-name,Values=pending,running,stopping,stopped |
    jq -c --arg r "$r" '.Reservations[].Instances[] | {region: $r, type: "instance", id: .InstanceId, tags: (.Tags // [])}'
  in_region "$r" ec2 describe-volumes --filters Name=status,Values=available |
    jq -c --arg r "$r" '.Volumes[] | {region: $r, type: "volume", id: .VolumeId, tags: (.Tags // [])}'
  in_region "$r" ec2 describe-network-interfaces --filters Name=status,Values=available |
    jq -c --arg r "$r" '.NetworkInterfaces[] | {region: $r, type: "network-interface", id: .NetworkInterfaceId, tags: (.TagSet // [])}'
  in_region "$r" ec2 describe-addresses |
    jq -c --arg r "$r" '.Addresses[] | {region: $r, type: "elastic-ip", id: .AllocationId, tags: (.Tags // [])}'
  in_region "$r" ec2 describe-nat-gateways --filter Name=state,Values=pending,available |
    jq -c --arg r "$r" '.NatGateways[] | {region: $r, type: "nat-gateway", id: .NatGatewayId, tags: (.Tags // [])}'
  vpcs="$(in_region "$r" ec2 describe-vpcs --filters "Name=tag:project,Values=$PROJECT" --query 'Vpcs[].VpcId')"
  vpcs="${vpcs:-[]}"
  # ELBv2 (NLB/ALB) and Classic ELB: list with their VPC, then tags in batches of 20.
  lbs="$(in_region "$r" elbv2 describe-load-balancers --query 'LoadBalancers[].{id: LoadBalancerArn, vpc: VpcId}')"
  if [ -n "$lbs" ] && [ "$(jq 'length' <<<"$lbs")" -gt 0 ]; then
    jq -r '.[].id' <<<"$lbs" | xargs -n 20 aws --region "$r" --output json elbv2 describe-tags --resource-arns |
      jq -c --arg r "$r" --argjson lbs "$lbs" --argjson vpcs "$vpcs" '.TagDescriptions[] | .ResourceArn as $id
        | {region: $r, type: "load-balancer", id: $id, tags: (.Tags // []),
           in_shopflow_vpc: ([$lbs[] | select(.id == $id) | .vpc] | length > 0 and (. - $vpcs | length == 0))}'
  fi
  lbs="$(in_region "$r" elb describe-load-balancers --query 'LoadBalancerDescriptions[].{id: LoadBalancerName, vpc: VPCId}')"
  if [ -n "$lbs" ] && [ "$(jq 'length' <<<"$lbs")" -gt 0 ]; then
    jq -r '.[].id' <<<"$lbs" | xargs -n 20 aws --region "$r" --output json elb describe-tags --load-balancer-names |
      jq -c --arg r "$r" --argjson lbs "$lbs" --argjson vpcs "$vpcs" '.TagDescriptions[] | .LoadBalancerName as $id
        | {region: $r, type: "classic-load-balancer", id: $id, tags: (.Tags // []),
           in_shopflow_vpc: ([$lbs[] | select(.id == $id) | .vpc] | length > 0 and (. - $vpcs | length == 0))}'
  fi
  local cluster
  for cluster in $(in_region "$r" eks list-clusters --query 'clusters[]' --output text); do
    in_region "$r" eks describe-cluster --name "$cluster" |
      jq -c --arg r "$r" '.cluster | {region: $r, type: "eks-cluster", id: .name, tags: ((.tags // {}) | to_entries | map({Key: .key, Value: .value}))}'
  done
}

delete_orphan() {
  local r="$1" type="$2" id="$3"
  case "$type" in
    instance) run in_region "$r" ec2 terminate-instances --instance-ids "$id" >/dev/null ;;
    volume) run in_region "$r" ec2 delete-volume --volume-id "$id" ;;
    network-interface) run in_region "$r" ec2 delete-network-interface --network-interface-id "$id" ;;
    elastic-ip) run in_region "$r" ec2 release-address --allocation-id "$id" ;;
    nat-gateway) run in_region "$r" ec2 delete-nat-gateway --nat-gateway-id "$id" >/dev/null ;;
    load-balancer) run in_region "$r" elbv2 delete-load-balancer --load-balancer-arn "$id" ;;
    classic-load-balancer) run in_region "$r" elb delete-load-balancer --load-balancer-name "$id" ;;
    *) return 1 ;; # EKS clusters need node groups removed first: cloud-down --force-api or the reaper
  esac
}

main() {
  parse_common_args "$@"
  set -- ${ARGS[@]+"${ARGS[@]}"}
  while [ $# -gt 0 ]; do
    case "$1" in
      --delete-tagged) DELETE_TAGGED=1 ;;
      --alert) ALERT=1 ;;
      --regions) REGIONS="${2:?--regions needs a value}"; shift ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done
  require_cmds aws jq xargs
  [ -n "$REGIONS" ] || REGIONS="$(aws_ ec2 describe-regions --query 'Regions[].RegionName' --output text)"

  local findings="" r
  for r in $REGIONS; do
    findings="$findings$(scan_region "$r")
"
  done
  findings="$(printf '%s' "$findings" | jq -c --arg p "$PROJECT" --arg c "$CLUSTER" "$OURS_JQ"' select(. != null) | . + {ours: ours($p; $c)}')"

  local unknown=0 remaining=0 line region type id ours
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    region="$(jq -r .region <<<"$line")"
    type="$(jq -r .type <<<"$line")"
    id="$(jq -r .id <<<"$line")"
    ours="$(jq -r .ours <<<"$line")"
    if [ "$ours" != true ] && [ "$(jq -r '.in_shopflow_vpc // false' <<<"$line")" = true ]; then
      log "UNTAGGED $region $type $id (in the shopflow VPC without project=$PROJECT: the reapers cannot delete it; remove it by hand)"
      unknown=$((unknown + 1))
    elif [ "$ours" != true ]; then
      log "UNKNOWN  $region $type $id (not shopflow; left alone)"
      unknown=$((unknown + 1))
    elif [ "$DELETE_TAGGED" = 1 ] && [ "$region" = "$REGION" ] && delete_orphan "$region" "$type" "$id"; then
      log "DELETED  $region $type $id"
    else
      log "ORPHAN   $region $type $id (shopflow; not deleted)"
      remaining=$((remaining + 1))
    fi
  done <<<"$findings"

  local total=$(($(printf '%s' "$findings" | grep -c . || true)))
  log "scanned: $(printf '%s' "$REGIONS" | wc -w | tr -d ' ') regions, $total billable resource(s): $unknown unknown, $remaining shopflow left"
  if [ "$ALERT" = 1 ] && [ $((unknown + remaining)) -gt 0 ]; then
    run aws_ sns publish --topic-arn "$(alert_topic_arn)" --subject "[shopflow] orphan check: $unknown unknown, $remaining left" \
      --message "$(printf '%s' "$findings" | jq -s '.')" >/dev/null || warn "could not publish the alert"
  fi
  [ "$unknown" -eq 0 ] || exit 2
  [ "$remaining" -eq 0 ] || exit 3
}

main "$@"
