#!/usr/bin/env bash
#
# Build the MCPForge Confidential Space workload image — F8-02a.
#
# The digest this prints is the value that gets pinned into
# `AttestationPolicy.image_digest`. Everything here exists to make that value a
# function of **content and nothing else**: a digest-pinned base, a hash-pinned
# dependency closure, no build secrets, a cold build cache, and a constant
# `SOURCE_DATE_EPOCH`.
#
# Two earlier attempts at reproducibility failed, and both failed the same way —
# a clock leaked into the digest:
#
#   1. `COPY` preserved source mtimes, so a fresh clone and a long-lived working
#      tree disagreed. Fixed in the Dockerfile, which normalises them to `@0`.
#   2. `SOURCE_DATE_EPOCH` was `git log -1 --pretty=%ct`, so an **empty commit**
#      changed the digest. Fixed below: it is a constant.
#
# Neither was caught by "build it twice and compare", because both builds shared
# the thing that varied.
#
# Usage:
#   ./build.sh                 build, load locally, print the digest
#   ./build.sh --push          the same, then push and verify the pushed digest
#   ./build.sh --check-base    report whether the pinned base digest has drifted
#
# It does not push unless --push is given. Nothing here logs in, creates a
# repository, or touches IAM: that is F8-02b.

set -euo pipefail

readonly REGISTRY="us-central1-docker.pkg.dev"
readonly PROJECT="mcpforge-aa5c2"
readonly REPOSITORY="mcpforge-executor"
# Artifact Registry addresses an image as REGISTRY/PROJECT/REPOSITORY/IMAGE.
# The ticket names the repository; `workload` is the image inside it.
readonly IMAGE_NAME="workload"
readonly IMAGE="${REGISTRY}/${PROJECT}/${REPOSITORY}/${IMAGE_NAME}"

# Confidential Space runs AMD SEV / Intel TDX on x86-64. A multi-platform image
# would also produce an index digest rather than a manifest digest, and the
# attestation claim carries one image digest.
readonly PLATFORM="linux/amd64"

readonly BASE_REF="python:3.12-slim-bookworm"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
readonly REPO_ROOT
readonly DOCKERFILE="${SCRIPT_DIR}/Dockerfile"

# Two overrides, and they exist for one reason: so that the reproducibility
# tests drive **this** script with **these** flags rather than a second copy of
# the buildx invocation that could drift from it. The defaults are the only
# thing an operator ever uses.
#
# `SOURCE_DATE_EPOCH` is a constant, so two runs over two contexts are
# comparable whatever repository they sit in.
readonly CONTEXT="${MCPFORGE_BUILD_CONTEXT:-${REPO_ROOT}}"
readonly OUT_DIR="${MCPFORGE_BUILD_OUTPUT:-${SCRIPT_DIR}/.build/oci}"
readonly ALTERNATE_OUTPUT="${MCPFORGE_BUILD_OUTPUT:+yes}"

die() { printf 'build.sh: %s\n' "$*" >&2; exit 1; }

require() { command -v "$1" >/dev/null 2>&1 || die "$1 is required and was not found"; }

# The build must not be able to pick up a credential. A build secret would end
# up inside the trust boundary, and the whole point of the image digest is that
# what it covers is knowable from the repository.
#
# There is no parser and no keyword list here. Both live in
# `dockerfile_scan.py`, which the test suite imports, because this guard and
# that suite previously carried one each and the two drifted until a credential
# could walk between them: `sed` joined continuations before dropping comment
# lines (Docker drops them first), `grep` was case-sensitive against
# case-insensitive instructions, and `[^#]*` stopped at the first `#` in the
# joined text. See the module docstring for the four measured escapes.
#
# It is also not the control. The control is the built artefact — the image
# config's `Env` and its instruction history, asserted in
# `services/api/tests/test_workload_image.py`. This runs first because failing
# before a build is cheaper than failing after one.
assert_no_build_secrets() {
  python3 "${SCRIPT_DIR}/dockerfile_scan.py" "${DOCKERFILE}" \
    || die "the Dockerfile appears to carry a build secret (see above)"
}

# A constant, not a timestamp.
#
# It was `git log -1 --pretty=%ct` — HEAD's commit time — which sounds
# reproducible and is not: `rewrite-timestamp=true` stamps build-created layers
# and the image config with it, so a single **empty commit** changed the digest.
# The reviewer measured exactly that, and it meant this ticket's own landing
# commit would have invalidated the digest it exists to produce.
#
# Zero is the natural value because the Dockerfile already normalises every
# workload file's mtime to `@0`. There is no clock anywhere in the digest now.
# `test_the_digest_does_not_depend_on_the_commit_timestamp` builds across two
# different HEADs and fails if one ever returns.
readonly SOURCE_DATE_EPOCH_CONSTANT=0

pinned_base_digest() {
  grep -m1 -oE 'sha256:[0-9a-f]{64}' "${DOCKERFILE}"
}

check_base() {
  require docker
  local pinned resolved
  pinned="$(pinned_base_digest)"
  resolved="$(docker buildx imagetools inspect "${BASE_REF}" --format '{{.Manifest.Digest}}')" \
    || die "could not resolve ${BASE_REF}; this step needs network access"
  printf 'pinned:   %s\n' "${pinned}"
  printf 'upstream: %s (%s)\n' "${resolved}" "${BASE_REF}"
  if [[ "${pinned}" != "${resolved}" ]]; then
    printf '\nThe base tag has moved. This is information, not an error: the pin is\n'
    printf 'deliberate. Updating it changes the workload digest and therefore\n'
    printf 'requires a new AttestationPolicy.image_digest and a new F8-02b\n'
    printf 'attribute condition.\n'
    return 1
  fi
  printf '\nThe pinned base is current.\n'
}

# The digest of the manifest in an OCI layout. This is the value a push would
# produce, which is why --push verifies it against what the registry reports
# rather than trusting either one alone.
oci_digest() {
  python3 - "$1" <<'PY'
import json, pathlib, sys
index = json.loads((pathlib.Path(sys.argv[1]) / "index.json").read_text())
manifests = index.get("manifests", [])
if len(manifests) != 1:
    sys.exit(f"expected exactly one manifest in the OCI layout, found {len(manifests)}")
print(manifests[0]["digest"])
PY
}

build() {
  local push="$1"
  require docker
  require python3
  docker buildx version >/dev/null 2>&1 || die "docker buildx is required"

  assert_no_build_secrets

  if [[ -n "${ALTERNATE_OUTPUT}" && "${push}" == "yes" ]]; then
    die "--push refuses to run against MCPFORGE_BUILD_OUTPUT; the pushed artefact \
must be the one built from the repository, not from a test context"
  fi

  # Set, never read from the environment: an inherited value would put a
  # caller's clock back into the digest.
  local epoch="${SOURCE_DATE_EPOCH_CONSTANT}"
  export SOURCE_DATE_EPOCH="${epoch}"

  rm -rf "${OUT_DIR}"
  mkdir -p "${OUT_DIR}"

  local -a common=(
    buildx build
    --file "${DOCKERFILE}"
    --platform "${PLATFORM}"
    # Attestations attached by buildx would change the digest and turn the
    # manifest into an index. The attestation that matters here is the
    # hardware one, not a build attestation.
    --provenance=false
    --sbom=false
  )

  common+=("${CONTEXT}")

  # Every build that produces a **pinnable** digest runs with a cold cache.
  #
  # This is not belt-and-braces. A stale BuildKit cache produced a real wrong
  # answer during this ticket: an earlier `deps` layer was reused after the
  # Dockerfile had been fixed, and `build.sh` printed a digest for an image the
  # current Dockerfile does not produce. That digest went into the README, where
  # it would have become `AttestationPolicy.image_digest`. A cache is a
  # performance optimisation; the value this script prints is a security
  # assertion, and the two should not share a code path.
  #
  # It costs a few seconds, because the pinned base is already local and the
  # dependency closure is small.
  local -a canonical=("${common[@]}" --no-cache)

  printf '==> Building %s (SOURCE_DATE_EPOCH=%s)\n' "${IMAGE}" "${epoch}"
  printf '==> Context: %s\n' "${CONTEXT}"

  # 1. Canonical build into an OCI layout with rewritten timestamps, from a
  #    cold cache. This is the artefact whose digest we report.
  docker "${canonical[@]}" \
    --output "type=oci,dest=${OUT_DIR},tar=false,rewrite-timestamp=true"

  local digest
  digest="$(oci_digest "${OUT_DIR}")"

  # 2. The same build, loaded into the local daemon so the tests in
  #    services/api/tests/test_workload_image.py can run it. This one may use
  #    the cache — it is warmed by step 1 and its digest is never pinned.
  #    Skipped for an alternate output, where the point is the digest and
  #    overwriting the local tag would be a side effect a test should not have.
  if [[ -z "${ALTERNATE_OUTPUT}" ]]; then
    docker "${common[@]}" --load --tag "${IMAGE}:local"
  fi

  if [[ "${push}" == "yes" ]]; then
    printf '==> Pushing %s\n' "${IMAGE}"
    docker "${canonical[@]}" \
      --output "type=image,name=${IMAGE}:latest,push=true,rewrite-timestamp=true,unpack=false"

    local pushed
    pushed="$(docker buildx imagetools inspect "${IMAGE}:latest" --format '{{.Manifest.Digest}}')"
    if [[ "${pushed}" != "${digest}" ]]; then
      die "pushed digest ${pushed} does not match the locally built digest ${digest}; \
the build is not reproducible and the pin would be meaningless"
    fi
    printf '==> Pushed digest verified against the local build.\n'
  else
    printf '==> Not pushed. Pass --push to publish.\n'
  fi

  printf '\n'
  printf 'image:  %s\n' "${IMAGE}"
  printf 'digest: %s\n' "${digest}"
  printf '\n'
  printf 'Pin this into AttestationPolicy.image_digest and into the F8-02b\n'
  printf 'attribute condition. A digest from an unpushed build is only usable\n'
  printf 'once the same digest has been verified in the registry by --push.\n'
}

main() {
  case "${1-}" in
    "")            build no ;;
    --push)        build yes ;;
    --check-base)  check_base ;;
    *)             die "unknown argument: $1 (expected --push, --check-base or nothing)" ;;
  esac
}

main "$@"
