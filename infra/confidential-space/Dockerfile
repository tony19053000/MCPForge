# MCPForge Confidential Space workload image — F8-02a.
#
# This image is the attested artifact. Its digest is the value pinned into
# `AttestationPolicy.image_digest`, so everything below sits INSIDE the trust
# boundary and every line is a security decision. See README.md.
#
# Build context is the repository root. Nothing is copied wholesale: every COPY
# names an explicit path, and `Dockerfile.dockerignore` excludes everything else
# from the context in the first place, so `.env` never reaches the daemon.

# ---------------------------------------------------------------------------
# Stage 1 — dependencies.
#
# Pinned by digest, never by tag: a tag is a mutable pointer, and an attested
# image built from a mutable base attests nothing about what is actually in it.
# The digest below is `python:3.12-slim-bookworm` as resolved on 2026-09-09;
# `build.sh --check-base` re-resolves it and reports a drift.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS deps

WORKDIR /build

# Hash-pinned, fully-resolved dependency closure. `--require-hashes` means a
# substituted or re-uploaded artifact fails the build rather than silently
# entering the trust boundary. `--no-deps` means the closure in the file is the
# closure installed — pip resolves nothing at build time.
COPY infra/confidential-space/requirements.txt ./requirements.txt
RUN python -m pip install \
        --no-cache-dir \
        --disable-pip-version-check \
        --require-hashes \
        --no-deps \
        --no-compile \
        --target /build/site-packages \
        -r ./requirements.txt \
    && find /build/site-packages -name '__pycache__' -type d -prune -exec rm -rf {} +

# The workload payload: the secure executor and the two modules it imports.
# Not the FastAPI service, not the API routers, not the agents, not the web
# tier. Each path is named individually so that adding a module to the trust
# boundary is an explicit edit to this file.
COPY services/api/src/mcpforge/__init__.py   /build/site-packages/mcpforge/__init__.py
COPY services/api/src/mcpforge/logging.py    /build/site-packages/mcpforge/logging.py
COPY services/api/src/mcpforge/execution/    /build/site-packages/mcpforge/execution/
COPY infra/confidential-space/entrypoint.py  /build/app/entrypoint.py

# Normalise every mtime to the epoch, and do it here rather than relying on the
# exporter.
#
# `COPY` preserves the *source* file's mtime, and `rewrite-timestamp=true` only
# clamps timestamps **newer** than `SOURCE_DATE_EPOCH` — it does not raise older
# ones. A fresh `git clone` writes files at checkout time, which is newer than
# HEAD's commit time, so they get clamped; a working tree that has held those
# files since before the last commit keeps their original mtimes, which do not.
# The two produce byte-identical file contents and different layer digests.
# The reviewer measured exactly that: two clean clones agreed with each other
# and disagreed with the working tree on one layer, with `diff -r` of the
# extracted layers empty.
#
# Pinning a digest that changes with a developer's checkout history would make
# every ordinary rebuild look like tampering. After this, the digest depends on
# content alone. `test_the_digest_does_not_depend_on_source_file_mtimes` builds
# from two contexts whose mtimes differ by five years and fails if they diverge.
RUN find /build/site-packages -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /build/site-packages /build/app -exec touch -h -d '@0' {} +

# ---------------------------------------------------------------------------
# Stage 2 — the runtime image. Same pinned base; nothing from pip's cache, no
# build tooling and no build arguments survive into it.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

# --- Confidential Space launch policy -------------------------------------
# Each label constrains what the VM operator may do to this workload at launch.
# The workload is attested by digest, so a policy that let an operator change
# its behaviour would make the digest a weaker statement than it looks. Every
# label is explained line by line in README.md §"Launch policy".
LABEL "tee.launch_policy.allow_capabilities"="false"
LABEL "tee.launch_policy.allow_cgroups"="false"
LABEL "tee.launch_policy.allow_cmd_override"="false"
LABEL "tee.launch_policy.allow_env_override"="MCPFORGE_RUN_ID,MCPFORGE_ATTESTATION_AUDIENCE"
LABEL "tee.launch_policy.log_redirect"="never"
LABEL "tee.launch_policy.monitoring_memory_allow"="never"
# `tee.launch_policy.allow_mount_destinations` is deliberately NOT declared.
# Its absence means no host mount destination is permitted, which is what we
# want; declaring it with an empty value would be a value, not an absence.

LABEL org.opencontainers.image.title="mcpforge-confidential-space-workload"
LABEL org.opencontainers.image.source="https://github.com/tony19053000/MCPForge"
LABEL org.opencontainers.image.description="MCPForge secure executor workload. Preflight, then obtains a Confidential Space attestation token and delivers it to the relying party; runs no repository job."

# A fixed uid/gid written directly rather than via `useradd`, which stamps
# /etc/shadow with the current day and would make the image unreproducible
# across dates. `nologin` because nothing in this image should ever get a shell.
RUN printf 'mcpforge:x:10001:\n' >> /etc/group \
    && printf 'mcpforge:x:10001:10001::/home/mcpforge:/usr/sbin/nologin\n' >> /etc/passwd \
    && mkdir -p /home/mcpforge /opt/mcpforge /workspace \
    && chown 10001:10001 /home/mcpforge /workspace

# Both come from the builder stage, where their mtimes were normalised. Copying
# `entrypoint.py` straight from the build context would reintroduce the
# checkout-dependent mtime this image exists to be free of.
COPY --from=deps --chown=10001:10001 /build/site-packages /opt/mcpforge/site-packages
COPY --from=deps --chown=10001:10001 /build/app/entrypoint.py /opt/mcpforge/entrypoint.py

ENV PYTHONPATH=/opt/mcpforge/site-packages \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/mcpforge

# The path jail root is NOT an environment variable. It is the constant
# `WORKSPACE_ROOT` in entrypoint.py — `/workspace`, created above — so it is
# neither operator-settable nor dependent on this image's ENV reaching the
# process at launch. The entrypoint refuses if `MCPFORGE_WORKSPACE_ROOT` is set.

# Non-root. 03_SECURITY_ACCESS.md §3. The entrypoint re-checks at runtime.
USER 10001:10001
WORKDIR /opt/mcpforge

# Exec form: no shell is interposed, so there is no `sh -c` to inject into and
# signals reach the workload directly. There is deliberately no CMD — there are
# no arguments to override, and `allow_cmd_override` is false in any case.
ENTRYPOINT ["python3", "/opt/mcpforge/entrypoint.py"]
