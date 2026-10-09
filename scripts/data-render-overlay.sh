#!/usr/bin/env bash
# Print `kubectl kustomize` of an sf-data overlay without its KSOPS generators.
# Usage: scripts/data-render-overlay.sh deploy/platform/<component>/<overlay>
# KSOPS Secrets can only be decrypted in the cluster (Argo CD repo-server) or with the owner's age key, which lanes
# and CI never use; everything else renders exactly as Argo CD renders it. Same approach as platform-validate.sh.
set -euo pipefail

overlay="${1:?usage: $0 deploy/platform/<component>/<overlay>}"
cd "$(dirname "$0")/.."
component_dir="$(dirname "$overlay")"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/$(dirname "$component_dir")"
cp -R "$component_dir" "$work/$component_dir"

while IFS= read -r kfile; do
  dir="$(dirname "$kfile")"
  for gen in $(yq '.generators[]?' "$kfile"); do
    if [[ "$(yq '.kind' "$dir/$gen")" == "ksops" ]]; then
      GEN="$gen" yq -i 'del(.generators[] | select(. == strenv(GEN)))' "$kfile"
    fi
  done
done < <(find "$work/$component_dir" -name kustomization.yaml)

kubectl kustomize "$work/$overlay"
