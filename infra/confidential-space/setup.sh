#!/usr/bin/env bash
#
# Stand up the Confidential Space workload identity for MCPForge — F8-02b.
#
# What this creates, and nothing else:
#
#   * three service APIs enabled: iam, sts, iamcredentials
#   * a workload identity pool
#   * an OIDC provider in that pool trusting the Confidential Space attestation
#     issuer, under an attribute condition pinning the image digest, the
#     hardware model and the debug status
#   * a dedicated workload service account
#   * the project roles enumerated in policy.md, and only those
#   * one binding of that pool's attested principals to that service account
#
# It is **safe by default**: with no arguments it plans and prints, and changes
# nothing. Mutating anything requires `--apply` explicitly.
#
#   ./setup.sh              plan only — print every command that --apply would run
#   ./setup.sh --apply      execute that plan, printing each command before it runs
#   ./setup.sh --verify     read-only: report drift, and any role beyond policy.md
#
# Idempotent by construction: every step reads the current state first and is a
# no-op when the desired state is already present, so a second `--apply` prints
# "No changes required." and issues no mutating call at all.
#
# It creates no service-account key. MCPForge does not use key files anywhere —
# see 03_SECURITY_ACCESS.md §9 — and workload identity federation is precisely
# the mechanism that makes one unnecessary. `gcloud iam service-accounts keys
# create` appears nowhere in this repository and a test asserts that.
#
# The image this pins is built by ./build.sh. Rebuilding with a changed
# Dockerfile changes the digest and therefore requires re-running this script,
# which will plan an update to the provider's attribute condition.

set -euo pipefail

# ── Canonical identifiers ───────────────────────────────────────────────────
# STATUS.md "Google Cloud identifiers — canonical" is the source of these.
# `launchforge-tee`, `launchforge-secure-executor`, `europe-west4` and
# `europe-docker.pkg.dev` are NOT MCPForge resources and must never appear here.
readonly PROJECT="mcpforge-aa5c2"
readonly REGION="us-central1"
readonly ARTIFACT_REPOSITORY="mcpforge-executor"

# Workload identity pools and providers are global resources; REGION above is
# the Artifact Registry and Confidential VM region and is printed for the
# operator, not passed to the pool.
readonly POOL_LOCATION="global"
readonly POOL_ID="mcpforge-confidential-space"
readonly PROVIDER_ID="mcpforge-attestation"

readonly SERVICE_ACCOUNT_ID="mcpforge-workload"
readonly SERVICE_ACCOUNT="${SERVICE_ACCOUNT_ID}@${PROJECT}.iam.gserviceaccount.com"

# Google's Confidential Space attestation issuer. The same constant is
# `CONFIDENTIAL_SPACE_ISSUER` in
# services/api/src/mcpforge/execution/attestation.py; F8-01 verifies `iss`
# against it and this provider federates it. A test asserts the two agree, so
# neither can drift alone.
readonly ISSUER_URI="https://confidentialcomputing.googleapis.com"

# The F8-02a workload image digest, recorded in README.md and reproduced by the
# reviewer from a clean clone. It is pinned by exact equality below; a prefix
# match, a `matches()` or a `!=` here would convert hardware attestation into no
# attestation at all, which is what this ticket's tests exist to prevent.
readonly IMAGE_DIGEST="sha256:31ed4925d78c88080870b5f0846956833a7ad5dd8f9f387997f423c71c5f1eb2"

# ── The attribute condition ─────────────────────────────────────────────────
#
# One definition, in clause order, joined below. Every clause here is written
# out in prose in policy.md beside the claim it constrains, and setup_scan.py
# refuses to let this script run when the two disagree — in either direction.
readonly -a CONDITION_CLAUSES=(
  "assertion.swname == 'CONFIDENTIAL_SPACE'"
  "assertion.submods.container.image_digest == '${IMAGE_DIGEST}'"
  "assertion.hwmodel in ['GCP_AMD_SEV', 'GCP_AMD_SEV_ES', 'GCP_AMD_SEV_SNP', 'GCP_INTEL_TDX']"
  "assertion.dbgstat == 'disabled-since-boot'"
  "'STABLE' in assertion.submods.confidential_space.support_attributes"
  "'${SERVICE_ACCOUNT}' in assertion.google_service_accounts"
)

join_clauses() {
  local joined=""
  local clause
  for clause in "${CONDITION_CLAUSES[@]}"; do
    if [[ -z "${joined}" ]]; then joined="${clause}"; else joined="${joined} && ${clause}"; fi
  done
  printf '%s' "${joined}"
}

ATTRIBUTE_CONDITION="$(join_clauses)"
readonly ATTRIBUTE_CONDITION

# Mapped attributes. `attribute.image_digest` is what the principalSet below
# selects on, so the pool grants the service account to attested workloads
# running exactly this image and to nothing else.
readonly ATTRIBUTE_MAPPING="google.subject=assertion.sub,\
attribute.image_digest=assertion.submods.container.image_digest,\
attribute.hwmodel=assertion.hwmodel,\
attribute.dbgstat=assertion.dbgstat,\
attribute.swname=assertion.swname"

# ── Roles ───────────────────────────────────────────────────────────────────
#
# Enumerated in policy.md with a justification each, and checked against it
# before this script does anything. `--verify` additionally reports any role the
# account holds that is *not* in this list, because least privilege is a
# statement about what is absent.
readonly -a PROJECT_ROLES=(
  "roles/confidentialcomputing.workloadUser"
  "roles/artifactregistry.reader"
)

readonly -a REQUIRED_SERVICES=(
  "iam.googleapis.com"
  "sts.googleapis.com"
  "iamcredentials.googleapis.com"
)

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly POLICY_DOC="${SCRIPT_DIR}/policy.md"
readonly SCAN="${SCRIPT_DIR}/setup_scan.py"

MODE="plan"
CHANGES=0
PROBLEMS=0

die() { printf 'setup.sh: %s\n' "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || die "$1 is required and was not found"; }
note() { printf '%s\n' "$*"; }
problem() { PROBLEMS=$((PROBLEMS + 1)); printf '  !! %s\n' "$*"; }

normalise() { python3 "${SCAN}" --normalise "$1"; }

# Every mutation in this script goes through here, so there is exactly one place
# that decides whether a command runs and exactly one place that prints it. The
# command is printed **before** it is executed, in both modes.
change() {
  local description="$1"
  shift
  CHANGES=$((CHANGES + 1))
  if [[ "${MODE}" == "apply" ]]; then
    printf '  ++ %s\n' "${description}"
  else
    printf '  would: %s\n' "${description}"
  fi
  printf '     $ %s\n' "$(printf '%q ' "$@")"
  if [[ "${MODE}" == "apply" ]]; then
    "$@"
  fi
}

# ── Preflight ───────────────────────────────────────────────────────────────

self_check() {
  local -a roles=()
  local role
  for role in "${PROJECT_ROLES[@]}"; do roles+=(--role "${role}"); done
  python3 "${SCAN}" --consistency "${POLICY_DOC}" --condition "${ATTRIBUTE_CONDITION}" \
    "${roles[@]}" \
    || die "setup.sh and policy.md disagree (above). Fix policy.md or the script; do not run either alone."
}

project_number() {
  gcloud projects describe "${PROJECT}" --format="value(projectNumber)" \
    || die "cannot read project ${PROJECT}; is gcloud authenticated for it?"
}

# ── Steps ───────────────────────────────────────────────────────────────────

step_services() {
  note ""
  note "Services"
  local enabled service
  enabled="$(gcloud services list --enabled --project "${PROJECT}" \
    --format="value(config.name)")" || die "cannot list enabled services on ${PROJECT}"
  for service in "${REQUIRED_SERVICES[@]}"; do
    if printf '%s\n' "${enabled}" | grep -Fxq "${service}"; then
      note "  ok: ${service} is already enabled"
    else
      change "enable ${service}" \
        gcloud services enable "${service}" --project "${PROJECT}"
    fi
  done
}

pool_exists() {
  gcloud iam workload-identity-pools describe "${POOL_ID}" \
    --location="${POOL_LOCATION}" --project "${PROJECT}" \
    --format="value(state)" 2>/dev/null
}

step_pool() {
  note ""
  note "Workload identity pool"
  local state
  if state="$(pool_exists)" && [[ -n "${state}" ]]; then
    if [[ "${state}" == "DELETED" ]]; then
      die "pool ${POOL_ID} exists but is DELETED. Undeleting is an operator decision \
(gcloud iam workload-identity-pools undelete); this script will not do it silently."
    fi
    note "  ok: pool ${POOL_ID} exists (${state})"
  else
    change "create workload identity pool ${POOL_ID}" \
      gcloud iam workload-identity-pools create "${POOL_ID}" \
      --location="${POOL_LOCATION}" --project "${PROJECT}" \
      --display-name="MCPForge Confidential Space" \
      --description="Attested MCPForge workloads running the F8-02a image"
  fi
}

provider_field() {
  gcloud iam workload-identity-pools providers describe "${PROVIDER_ID}" \
    --workload-identity-pool="${POOL_ID}" --location="${POOL_LOCATION}" \
    --project "${PROJECT}" --format="value($1)" 2>/dev/null
}

step_provider() {
  note ""
  note "OIDC provider"
  local state
  if ! state="$(provider_field state)" || [[ -z "${state}" ]]; then
    change "create OIDC provider ${PROVIDER_ID} trusting ${ISSUER_URI}" \
      gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_ID}" \
      --workload-identity-pool="${POOL_ID}" --location="${POOL_LOCATION}" \
      --project "${PROJECT}" \
      --display-name="Confidential Space attestation" \
      --description="Confidential Space attestation tokens for the MCPForge workload image" \
      --issuer-uri="${ISSUER_URI}" \
      --attribute-mapping="${ATTRIBUTE_MAPPING}" \
      --attribute-condition="${ATTRIBUTE_CONDITION}"
    return
  fi
  if [[ "${state}" == "DELETED" ]]; then
    die "provider ${PROVIDER_ID} exists but is DELETED; undeleting is an operator decision."
  fi

  # An existing provider is compared field by field. A provider whose condition
  # has drifted is the failure mode this whole ticket is about, so it is
  # corrected rather than reported and left.
  local live_condition live_mapping live_issuer drifted="no"
  live_condition="$(normalise "$(provider_field attributeCondition)")"
  live_mapping="$(normalise "$(provider_field 'attributeMapping.list(separator=",")')")"
  live_issuer="$(normalise "$(provider_field oidc.issuerUri)")"

  if [[ "${live_condition}" != "$(normalise "${ATTRIBUTE_CONDITION}")" ]]; then
    note "  drift: attribute condition"
    note "    live:    ${live_condition}"
    note "    desired: ${ATTRIBUTE_CONDITION}"
    drifted="yes"
  fi
  if [[ "${live_issuer}" != "${ISSUER_URI}" ]]; then
    note "  drift: issuer is '${live_issuer}', expected '${ISSUER_URI}'"
    drifted="yes"
  fi
  if ! mappings_match "${live_mapping}"; then
    note "  drift: attribute mapping is '${live_mapping}'"
    drifted="yes"
  fi

  if [[ "${drifted}" == "no" ]]; then
    note "  ok: provider ${PROVIDER_ID} matches the declared issuer, mapping and condition"
  else
    change "update OIDC provider ${PROVIDER_ID} to the declared mapping and condition" \
      gcloud iam workload-identity-pools providers update-oidc "${PROVIDER_ID}" \
      --workload-identity-pool="${POOL_ID}" --location="${POOL_LOCATION}" \
      --project "${PROJECT}" \
      --issuer-uri="${ISSUER_URI}" \
      --attribute-mapping="${ATTRIBUTE_MAPPING}" \
      --attribute-condition="${ATTRIBUTE_CONDITION}"
  fi
}

# gcloud reports the mapping as an unordered map, so compare as sets of
# `key=value` pairs rather than as one string.
mappings_match() {
  local live="$1" pair
  local -a desired_pairs
  IFS=',' read -r -a desired_pairs <<<"${ATTRIBUTE_MAPPING}"
  for pair in "${desired_pairs[@]}"; do
    printf '%s' "${live}" | tr ',' '\n' | grep -Fxq "${pair}" || return 1
  done
  local live_count desired_count
  live_count="$(printf '%s' "${live}" | tr ',' '\n' | grep -c . || true)"
  desired_count="${#desired_pairs[@]}"
  [[ "${live_count}" == "${desired_count}" ]]
}

step_service_account() {
  note ""
  note "Workload service account"
  if gcloud iam service-accounts describe "${SERVICE_ACCOUNT}" \
    --project "${PROJECT}" --format="value(email)" >/dev/null 2>&1; then
    note "  ok: ${SERVICE_ACCOUNT} exists"
  else
    change "create service account ${SERVICE_ACCOUNT}" \
      gcloud iam service-accounts create "${SERVICE_ACCOUNT_ID}" \
      --project "${PROJECT}" \
      --display-name="MCPForge Confidential Space workload" \
      --description="Runs the attested MCPForge workload image. No keys; workload identity only."
  fi
}

# `role<TAB>member` for every binding on the project. Read once, matched by
# exact string equality in bash, so no `--filter` expression has to be trusted
# to interpret a `principalSet://` member correctly.
project_bindings() {
  gcloud projects get-iam-policy "${PROJECT}" \
    --flatten="bindings[].members" \
    --format="value(bindings.role,bindings.members)"
}

held_project_roles() {
  local role member
  while IFS=$'\t' read -r role member; do
    [[ "${member}" == "serviceAccount:${SERVICE_ACCOUNT}" ]] && printf '%s\n' "${role}"
  done < <(project_bindings)
  return 0
}

step_project_roles() {
  note ""
  note "Project roles"
  local held role
  held="$(held_project_roles)" || die "cannot read the IAM policy of ${PROJECT}"
  for role in "${PROJECT_ROLES[@]}"; do
    if printf '%s\n' "${held}" | grep -Fxq "${role}"; then
      note "  ok: ${SERVICE_ACCOUNT} already holds ${role}"
    else
      change "grant ${role} to ${SERVICE_ACCOUNT}" \
        gcloud projects add-iam-policy-binding "${PROJECT}" \
        --member="serviceAccount:${SERVICE_ACCOUNT}" \
        --role="${role}" \
        --condition=None
    fi
  done
}

principal_set() {
  printf 'principalSet://iam.googleapis.com/projects/%s/locations/%s/workloadIdentityPools/%s/attribute.image_digest/%s' \
    "$1" "${POOL_LOCATION}" "${POOL_ID}" "${IMAGE_DIGEST}"
}

step_pool_binding() {
  note ""
  note "Pool → service account binding"
  local member held
  member="$(principal_set "$1")"
  if held="$(gcloud iam service-accounts get-iam-policy "${SERVICE_ACCOUNT}" \
    --project "${PROJECT}" --flatten="bindings[].members" \
    --format="value(bindings.role,bindings.members)" 2>/dev/null)" \
    && printf '%s\n' "${held}" | grep -Fxq "$(printf 'roles/iam.workloadIdentityUser\t%s' "${member}")"; then
    note "  ok: the attested principal set already holds roles/iam.workloadIdentityUser"
  else
    change "let attested workloads running ${IMAGE_DIGEST} impersonate ${SERVICE_ACCOUNT}" \
      gcloud iam service-accounts add-iam-policy-binding "${SERVICE_ACCOUNT}" \
      --project "${PROJECT}" \
      --role="roles/iam.workloadIdentityUser" \
      --member="${member}"
  fi
}

# ── Verify ──────────────────────────────────────────────────────────────────
#
# Read-only. "Least privilege" is a claim about what is *absent*, so the check
# that matters most here is the extra-role one: a role granted by hand, or by a
# future edit to PROJECT_ROLES that policy.md did not follow, is reported and
# makes this exit non-zero.
step_verify() {
  note ""
  note "Verification (read-only)"
  local held role condition

  if ! gcloud iam service-accounts describe "${SERVICE_ACCOUNT}" \
    --project "${PROJECT}" --format="value(email)" >/dev/null 2>&1; then
    problem "service account ${SERVICE_ACCOUNT} does not exist"
  fi

  held="$(held_project_roles)" || die "cannot read the IAM policy of ${PROJECT}"
  for role in "${PROJECT_ROLES[@]}"; do
    printf '%s\n' "${held}" | grep -Fxq "${role}" \
      || problem "missing role ${role} on ${SERVICE_ACCOUNT}"
  done
  while read -r role; do
    [[ -z "${role}" ]] && continue
    printf '%s\n' "${PROJECT_ROLES[@]}" | grep -Fxq "${role}" \
      || problem "role ${role} is held by ${SERVICE_ACCOUNT} but is not enumerated in policy.md"
  done <<<"${held}"

  condition="$(normalise "$(provider_field attributeCondition)")"
  if [[ -z "${condition}" ]]; then
    problem "provider ${PROVIDER_ID} has no attribute condition, or does not exist"
  elif [[ "${condition}" != "$(normalise "${ATTRIBUTE_CONDITION}")" ]]; then
    problem "the live attribute condition is not the declared one: ${condition}"
  else
    note "  ok: the live attribute condition is exactly the declared one"
  fi

  if [[ "${PROBLEMS}" -gt 0 ]]; then
    note ""
    die "${PROBLEMS} problem(s) found. Nothing was changed; --verify is read-only."
  fi
  note "  ok: no drift and no role beyond policy.md"
}

# ── Main ────────────────────────────────────────────────────────────────────

banner() {
  note "MCPForge Confidential Space workload identity — F8-02b"
  note ""
  note "  mode:            ${MODE}"
  note "  project:         ${PROJECT} (number ${1})"
  note "  region:          ${REGION} (Artifact Registry repository ${ARTIFACT_REPOSITORY})"
  note "  pool:            ${POOL_ID} (${POOL_LOCATION})"
  note "  provider:        ${PROVIDER_ID}"
  note "  issuer:          ${ISSUER_URI}"
  note "  service account: ${SERVICE_ACCOUNT}"
  note "  image digest:    ${IMAGE_DIGEST}"
  note ""
  note "  attribute condition:"
  local clause
  for clause in "${CONDITION_CLAUSES[@]}"; do
    note "    ${clause}"
  done
}

main() {
  case "${1-}" in
  "") MODE="plan" ;;
  --apply) MODE="apply" ;;
  --verify) MODE="verify" ;;
  *) die "unknown argument: $1 (expected --apply, --verify or nothing)" ;;
  esac
  readonly MODE

  require gcloud
  require python3
  self_check

  local number
  number="$(project_number)"
  banner "${number}"

  if [[ "${MODE}" == "verify" ]]; then
    step_verify
    note ""
    note "Verified. No changes were made."
    return 0
  fi

  step_services
  step_pool
  step_provider
  step_service_account
  step_project_roles
  step_pool_binding "${number}"

  note ""
  if [[ "${CHANGES}" -eq 0 ]]; then
    note "No changes required."
  elif [[ "${MODE}" == "apply" ]]; then
    note "${CHANGES} change(s) applied. Re-run with --verify to check for drift."
  else
    note "${CHANGES} change(s) would be made. Nothing was changed."
    note "Re-run with --apply to execute exactly the commands printed above."
  fi
}

main "$@"
