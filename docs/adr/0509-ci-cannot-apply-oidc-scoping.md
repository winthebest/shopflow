# 0509. CI cannot apply infrastructure; OIDC roles are scoped to one workflow

- Status: Accepted
- Date: 2026-10-09
- Lane: sf-cloud

## Context

Any role a pull request can assume is reachable by whoever can edit a workflow file in that pull request. An
apply-capable CI role would turn a malicious or careless PR into account takeover or a cost incident.

## Decision

- No long-lived AWS keys anywhere: GitHub Actions uses OIDC. The repository customizes the `sub` claim to include
  `job_workflow_ref`, so each role trusts exactly one workflow file and ref.
- `shopflow-ci-plan`: same-repo pull requests running `infra-ci.yml`; allow-list of describe/get/list plus state
  object reads; explicit Deny of `ssm:GetParameter*`, `kms:Decrypt`, `secretsmanager:GetSecretValue`, data bucket
  objects and state writes. Runs `tofu plan -lock=false`. Off until `INFRA_PLAN_ENABLED=true`.
- `shopflow-reaper`: only `cloud-reaper.yml` on `refs/heads/main`; deletes only `project=shopflow` session compute;
  writes and locks only the layer-2 state key; explicit Deny of IAM create/attach/pass and of layer-0 resources.
- `cloud-up`/`cloud-down` and every apply run from the operator's laptop; layer 0 only with the admin permission set.
- Every role carries the `shopflow-boundary` permissions boundary (one region, no long-lived credentials, guardrails
  cannot be modified).

## Alternatives considered

| Option | Why not |
|---|---|
| Apply on merge to main | a merged mistake creates or destroys billable resources unattended |
| Default `sub` (`repo:<repo>:ref:...`) | any workflow in the repo could assume the role |

Limit of the `job_workflow_ref` pin for `ci-plan`: on a pull request it names the PR's **own** copy of
`infra-ci.yml` (`…@refs/pull/<n>/merge`). Anyone who can push a branch to this repository can change that file in
their PR and run arbitrary steps with `ci-plan` credentials. The pin only stops other workflows and forks. So
`ci-plan` must stay read-only forever: never grant it a write action, `ssm:GetParameter*`, `kms:Decrypt`, secrets,
data objects or state writes, whatever a future plan seems to need. The reaper is not exposed this way: it trusts
only `refs/heads/main`, which needs a reviewed merge.

## Consequences

- Positive: a compromised PR can at most read infrastructure metadata; the reaper can only remove session compute.
- Negative: OIDC `sub` customization is a repository setting the user must apply; plans in CI need layer 0 first.
- When to revisit: if multiple people need to apply (then: protected environment + manual approval).
