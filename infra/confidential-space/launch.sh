#!/usr/bin/env bash
#
# Boot one Confidential Space VM running the MCPForge workload — F8-02.
#
# The run id and audience come from the MCPForge API, the relying party that
# will verify the token. It issued them; this script only reads them:
#
#   (cd services/api && uv run python -m mcpforge.relying_party begin)
#   ./launch.sh --run-id <id>             plan only — print the command; run nothing
#   ./launch.sh --run-id <id> --apply     create exactly the VM printed
#   (cd services/api && uv run python -m mcpforge.relying_party verify <id>)
#
# There is no way to give this script an audience. It asks the relying party
# (`relying_party show`) for the record of the run id, and refuses unless that
# run was issued, is unverified and is in date. A VM booted with any other
# audience would produce a token the relying party never accepts.
#
# It is plan-only by default, exactly like setup.sh: without --apply it calls no
# gcloud command at all. Creating a VM spends money and is the project owner's
# decision; nothing in this repository runs --apply.
#
# The workload obtains a token and writes it to
# gs://mcpforge-aa5c2-attestation/attestation/<run id>.jwt. It verifies nothing
# itself, so its exit status is a self-report about delivery, not attestation.
# `log_redirect=never` keeps stdout inside the TEE; the launcher reports the
# status on the serial console as "workload task ended and returned N":
#
#   0      token obtained and delivered (self-report; verify with the API)
#   16     no token was obtained from the launcher
#   17     a token was obtained but not delivered
#   10-15  a preflight refusal (see README.md)
#
# THE IMAGE FAMILY IS THE PRODUCTION ONE, `confidential-space`. It reports
# `dbgstat` as `disabled-since-boot`, which the relying party requires. The debug
# family, `confidential-space-debug`, reports `enabled`, so a debug run can never
# verify: `relying_party verify` refuses it with DEBUG_MODE_ENABLED. An earlier
# version launched the debug family; the F8-02 review found a paid run on it
# could not clear B-04, and this was changed before any money was spent on it.
#
# A production VM still writes the launcher's messages, including the exit
# status below, to the serial console, and it stops when the workload ends — a
# debug VM keeps running and billing.
#
# Read it:  gcloud compute instances get-serial-port-output <instance> \
#             --project=mcpforge-aa5c2 --zone=us-central1-b

set -euo pipefail

# ── Canonical identifiers ───────────────────────────────────────────────────
# STATUS.md "Google Cloud identifiers — canonical" is the source of these.
# `launchforge-tee`, `launchforge-secure-executor`, `europe-west4` and
# `europe-docker.pkg.dev` are NOT MCPForge resources and must never appear here.
readonly PROJECT="mcpforge-aa5c2"
readonly ZONE="us-central1-b"
readonly IMAGE_REPOSITORY="us-central1-docker.pkg.dev/${PROJECT}/mcpforge-executor/workload"
readonly SERVICE_ACCOUNT="mcpforge-workload@${PROJECT}.iam.gserviceaccount.com"

# The workload image digest: the same value as setup.sh's IMAGE_DIGEST, README.md
# and the API's CONFIDENTIAL_SPACE_IMAGE_DIGEST, and a test fails if launch.sh
# and setup.sh disagree. It selects which image boots; it is not what the
# relying party trusts — the API pins its own copy. The image is referenced by
# digest, never by tag: the launcher passes `tee-image-reference` verbatim to
# containerd's pull, which accepts `name@sha256:...`.
readonly IMAGE_DIGEST="sha256:cebf7ea1fcb0e898142041507c3a77b7590651a2fb03f14e8f82be548ef89765"

readonly MACHINE_TYPE="n2d-standard-2"
readonly CONFIDENTIAL_COMPUTE_TYPE="SEV"
readonly IMAGE_PROJECT="confidential-space-images"
readonly IMAGE_FAMILY="confidential-space"

die() { printf 'launch.sh: %s\n' "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || die "$1 is required and was not found"; }

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly API_DIR="${SCRIPT_DIR}/../../services/api"

# The relying party's record of a run, or a refusal. Exactly two lines, in order.
issued_run() {
  (cd -- "${API_DIR}" && uv run --quiet python -m mcpforge.relying_party show "$1")
}

# A value is printed inside single quotes. None of the values here can contain
# one — they are fixed identifiers and hex — and this refuses rather than
# printing a command whose quoting would change its meaning.
quote() {
  [[ "$1" != *"'"* ]] || die "refusing to quote a value containing a single quote: $1"
  printf "'%s'" "$1"
}

main() {
  local mode="plan" run_id=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
    --apply) mode="apply" ;;
    --run-id)
      [[ $# -ge 2 ]] || die "--run-id needs a value"
      run_id="$2"
      shift
      ;;
    *) die "unknown argument: $1 (expected --run-id <id> and optionally --apply)" ;;
    esac
    shift
  done
  [[ -n "${run_id}" ]] || die "--run-id is required. Issue a run with: \
(cd services/api && uv run python -m mcpforge.relying_party begin)"

  require uv
  local issued
  issued="$(issued_run "${run_id}")" \
    || die "the relying party did not issue run ${run_id}, or it is verified or expired; issue a new one"

  local -a lines
  mapfile -t lines <<<"${issued}"
  [[ ${#lines[@]} -eq 2 \
    && "${lines[0]}" == "MCPFORGE_RUN_ID=${run_id}" \
    && "${lines[1]}" == MCPFORGE_ATTESTATION_AUDIENCE=* ]] \
    || die "unexpected answer from the relying party for run ${run_id}"
  local audience="${lines[1]#MCPFORGE_ATTESTATION_AUDIENCE=}"
  local instance="mcpforge-${run_id}"
  [[ "${instance}" =~ ^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$ ]] \
    || die "run id ${run_id} cannot name a VM instance"

  # `^~^` makes `~` the metadata delimiter, so a value may contain a comma.
  # No value may contain `~`; the run id, audience and digest cannot.
  local metadata="^~^tee-image-reference=${IMAGE_REPOSITORY}@${IMAGE_DIGEST}"
  metadata+="~tee-env-MCPFORGE_RUN_ID=${run_id}"
  metadata+="~tee-env-MCPFORGE_ATTESTATION_AUDIENCE=${audience}"

  # The one command. It is printed from this array and, with --apply, executed
  # from this same array, so what is printed is exactly what runs.
  local -a create=(
    gcloud compute instances create "${instance}"
    "--project=${PROJECT}"
    "--zone=${ZONE}"
    "--confidential-compute-type=${CONFIDENTIAL_COMPUTE_TYPE}"
    "--machine-type=${MACHINE_TYPE}"
    "--maintenance-policy=TERMINATE"
    "--shielded-secure-boot"
    "--image-project=${IMAGE_PROJECT}"
    "--image-family=${IMAGE_FAMILY}"
    "--service-account=${SERVICE_ACCOUNT}"
    "--scopes=cloud-platform"
    "--metadata=${metadata}"
  )

  printf 'MCPForge Confidential Space launch — F8-02\n\n'
  printf '  mode:          %s\n' "${mode}"
  printf '  project:       %s\n' "${PROJECT}"
  printf '  zone:          %s\n' "${ZONE}"
  printf '  instance:      %s\n' "${instance}"
  printf '  image:         %s@%s\n' "${IMAGE_REPOSITORY}" "${IMAGE_DIGEST}"
  printf '  image family:  %s (production: dbgstat disabled-since-boot)\n' "${IMAGE_FAMILY}"
  printf '  run id:        %s (issued by the MCPForge API)\n' "${run_id}"
  printf '  audience:      %s (the nonce the relying party chose)\n\n' "${audience}"

  printf '# >>> launch command\n'
  local index last=$((${#create[@]} - 1))
  for index in "${!create[@]}"; do
    if ((index == 0)); then
      printf '%s' "${create[index]}"
    elif ((index < 4)); then
      # `compute instances create` — fixed words, nothing to quote.
      printf ' %s' "${create[index]}"
    elif ((index == 4)); then
      printf ' %s' "$(quote "${create[index]}")"
    else
      printf ' \\\n  %s' "$(quote "${create[index]}")"
    fi
    ((index == last)) && printf '\n'
  done
  printf '# <<< launch command\n\n'

  if [[ "${mode}" != "apply" ]]; then
    printf 'Plan only. Nothing was run and no gcloud command was called.\n'
    printf 'Re-run with --apply to create exactly the VM printed above.\n'
    return 0
  fi

  require gcloud
  printf 'Creating %s ...\n' "${instance}"
  "${create[@]}"
}

main "$@"
