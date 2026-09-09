# Confidential Space workload image — `F8-02a`

The container image that runs inside Google Confidential Space, and **the
artefact the attestation is about**. Its digest is the value pinned into
`AttestationPolicy.image_digest` (`services/api/src/mcpforge/execution/attestation.py`)
and, at `F8-02b`, into the workload identity pool's attribute condition.

That single fact drives every decision in here. A hardware attestation says
"this exact image ran on genuine confidential hardware". It says nothing about
whether the image was a good idea. Anything baked in is inside the trust
boundary, so the rule is: put in as little as possible, know exactly what is
there, and prove it by test rather than by reading the Dockerfile.

---

## Status — read this before using anything below

| | |
|---|---|
| Image builds | Yes, reproducibly, from a clean checkout |
| Image pushed to Artifact Registry | **No.** Nothing has been pushed. The registry is still empty |
| Digest below | A **local** build digest, not a registry digest |
| Attestation token obtained | No. The workload obtains none, and none is simulated |
| Repository job executed | No. There is no job runner in this image |
| Blocker `B-04` | Still open. This ticket narrows it; `F8-02b` and `F8-02` close it |

The image performs **preflight only**: it validates its configuration and its
runtime environment, prints a JSON record, and exits. Obtaining an attestation
token and running a repository job is `F8-02`, which is `BLOCKED` on `B-04`.
`entrypoint.py` says so in its output (`attestation_token_obtained: false`,
`job_runner_present: false`) rather than leaving a reader to infer it.

---

## The digest

```
image:  us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor/workload
digest: sha256:9dffebfbde81d889a4e93b327bcc25e2b65bf5670f1e50408e2bee803fb3b7d3
```

Produced by `./build.sh` on 2026-09-09 from a cold BuildKit cache, and
reproduced by a second cold build, from a second checkout with mtimes five years
apart, and across two different HEADs.

**This digest is not yet the value to pin.** It is the digest of the OCI layout
`build.sh` writes locally. It is *intended* to equal the digest Artifact
Registry reports after a push — the exporter settings are identical and the
build is a function of content alone (see "Reproducibility" below) — but
"intended to equal" is not "verified to equal". `./build.sh --push` performs that comparison and aborts if
the two differ. Until someone has run it and this line has been updated with the
registry's own answer, do not put this value into `AttestationPolicy` or into an
IAM attribute condition.

Size: **210 MB** on disk, **48.5 MiB** compressed across 8 layers, of which the
base image is 189 MB. Almost all of MCPForge's ~21 MB is the `cryptography`
wheel, which `pyjwt[crypto]` needs to verify an RS256 attestation token.

The registry path deviates from the ticket in one detail, noted so it is not
mistaken for drift: the ticket names
`us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor`, which is the
Artifact Registry *repository*. An image reference needs a name inside that
repository, so the image is `.../mcpforge-executor/workload`.

---

## What is in the image, and what is deliberately not

**In:**

- `mcpforge.execution` — `attestation.py`, `provider.py`, `development.py`
- `mcpforge.logging` — imported by the executor
- `entrypoint.py` — this directory's preflight
- `anyio`, `structlog`, `pyjwt[crypto]` and their transitive closure, installed
  from `requirements.txt` with `--require-hashes`

**Out, and each for a reason:**

- **FastAPI, uvicorn, the API routers.** The workload does not serve HTTP. An
  HTTP server inside a TEE is an inbound attack surface inside the trust
  boundary.
- **The agents and `google-genai`.** The workload does not call a model. Gemini
  is called from the API service, outside this image, and no Gemini key is
  anywhere near here.
- **The GitHub client.** The workload holds no installation token.
- **The web tier.** Not a workload at all.
- **Any credential.** No `.env`, no ADC file, no service-account key, no build
  secret. `test_no_layer_contains_credential_material` asserts this against the
  real layers.

`test_the_image_carries_the_executor_and_not_the_service` asserts both halves:
the executor is present, and none of the above is.

---

## Launch policy — line by line

Confidential Space reads these labels from the image and uses them to constrain
what the VM *operator* may do to the workload at launch. They matter because the
attestation pins the image; if an operator could change what that image does,
the pin would be a weaker statement than it appears. Every value below is
asserted against the built image by
`test_the_launch_policy_labels_are_exactly_as_documented`, and that test also
fails if a label is added here without being documented.

```dockerfile
LABEL "tee.launch_policy.allow_capabilities"="false"
```
The operator may not grant the container additional Linux capabilities.
Defaults to `false`; declared explicitly because a security-relevant default is
one edit away from not being the default. The workload needs no capability
beyond the ordinary set — it does not bind a port, mount anything, or manage
devices.

```dockerfile
LABEL "tee.launch_policy.allow_cgroups"="false"
```
The operator may not set cgroup parameters. Cgroup control is a lever on
scheduling and memory pressure, which is a side channel against a workload
processing private source code, and the workload has no need for it.

```dockerfile
LABEL "tee.launch_policy.allow_cmd_override"="false"
```
The most important one. With `true`, an operator could keep this exact image —
and therefore this exact attested digest — while running a completely different
command inside it. `python3 -c "..."` in place of the entrypoint would attest
the image and execute something else. `false` means the digest and the behaviour
are the same statement.

```dockerfile
LABEL "tee.launch_policy.allow_env_override"="MCPFORGE_RUN_ID,MCPFORGE_ATTESTATION_AUDIENCE"
```
An allowlist, and it is short on purpose. These two are genuinely per-run: the
run id correlates the job with the orchestrator's run, and the audience is the
nonce that the attestation token must carry as its single audience, which is
what stops a token from one run being replayed into another.

Note what is *absent*: `MCPFORGE_WORKSPACE_ROOT`. It is baked into the image as
an `ENV`, so it is present, but an operator cannot change it. It is the path
jail root; an operator who could set it to `/` would move the jail rather than
escape it, which is the same outcome by a politer route.

```dockerfile
LABEL "tee.launch_policy.log_redirect"="never"
```
The workload's stdout and stderr are never redirected to the operator's Cloud
Logging. The default, `debugonly`, redirects when a debug image is used. This
process handles a developer's private source code; anything it prints leaves the
TEE if redirected, and Cloud Logging is outside the boundary.

The cost is real and is accepted: you cannot debug this workload by reading its
logs. Debugging means building a variant with `log_redirect="debugonly"`, which
has a **different digest** and therefore fails the production attestation
policy — which is the correct outcome, not an inconvenience to work around.

```dockerfile
LABEL "tee.launch_policy.monitoring_memory_allow"="never"
```
No memory-usage telemetry to the operator's project. Memory usage over time is a
weak but real side channel on what a workload is processing, and the workload
does not need to be monitored to be correct. Same trade as the line above.

```dockerfile
# tee.launch_policy.allow_mount_destinations is deliberately NOT declared.
```
Undeclared means no host mount destination is permitted, which is what we want:
the workload's filesystem should be exactly the attested image plus its
ephemeral workspace. Declaring the label with an empty value would be supplying
a value rather than withholding one, so it is omitted instead.

---

## The Dockerfile, decision by decision

**Base pinned by digest, not by tag.**
`python:3.12-slim-bookworm@sha256:782412e8…`. A tag is a mutable pointer. An
image built on a tag has an unknowable base, and attesting it attests nothing
about what is actually inside. `test_every_base_image_is_pinned_by_digest`
checks *both* stages, because a builder stage on a moving base puts unknown
bytes into the trust boundary just as effectively as the final stage would.

`./build.sh --check-base` re-resolves the tag and reports drift. Drift is
information, not an error: updating the pin changes the workload digest, which
means a new `AttestationPolicy.image_digest` and a new `F8-02b` attribute
condition.

**Dependencies installed with `--require-hashes --no-deps`.**
`requirements.txt` is a fully-resolved closure with hashes, generated by
`uv pip compile --generate-hashes` from the versions in `services/api/uv.lock`.
A substituted or re-uploaded artefact fails the build instead of quietly
entering the trust boundary, and pip resolves nothing at build time.

**No `COPY . .`, ever.** Every `COPY` names an explicit path, so adding a module
to the trust boundary is a visible edit.
`test_nothing_is_copied_wholesale_into_the_image` enforces it.

**`Dockerfile.dockerignore` is an allowlist.** The build context is the
repository root, which contains `.env`. The ignore file excludes `*` and then
re-includes the handful of paths the Dockerfile copies. Excluding known-bad
names is a list that goes stale; excluding everything is not.

**No build secrets.** No `ARG` or `ENV` with a secret-shaped name, no
`--mount=type=secret`. See "Build secrets, and why the Dockerfile is no longer
the thing checked" below — the short version is that the Dockerfile text is
checked as defence in depth and the **built image** is the control.

**Non-root, by a numeric uid.** `USER 10001:10001`, written into `/etc/passwd`
with `printf` rather than created with `useradd`, because `useradd` stamps
`/etc/shadow` with the current day and would make the image unreproducible from
one date to the next. The shell is `/usr/sbin/nologin`.

**Exec-form `ENTRYPOINT`, no `CMD`.** `ENTRYPOINT ["python3", "/opt/mcpforge/entrypoint.py"]`.
Shell form would run under `sh -c`, which interposes a shell inside the trust
boundary and breaks signal delivery. There is no `CMD` because there is nothing
to override — and `allow_cmd_override` is `false` regardless.

---

## `entrypoint.py` — refusals, not defaults

The acceptance criterion is that it *refuses to start when required
configuration is absent rather than starting with defaults*. There is no
fallback value for any required name anywhere in the file.

| Exit | Reason | Fires when |
|---|---|---|
| `0` | `PREFLIGHT_OK` | every precondition held |
| `10` | `CONFIG_MISSING` | a required variable is absent, empty, or whitespace |
| `11` | `CONFIG_INVALID` | the run id is not a safe path component, or the audience is padded or too short to be a nonce |
| `12` | `RUNNING_AS_ROOT` | `geteuid() == 0` |
| `13` | `CREDENTIAL_PRESENT` / `CREDENTIAL_STATE_UNKNOWN` | credential material is reachable, or its presence could not be determined |
| `14` | `WORKSPACE_UNUSABLE` | the path jail root is missing or not writable |
| `15` | `WORKLOAD_PAYLOAD_MISSING` | the secure executor did not import |

Required configuration: `MCPFORGE_RUN_ID`, `MCPFORGE_ATTESTATION_AUDIENCE`,
`MCPFORGE_WORKSPACE_ROOT`. Whitespace-only counts as absent, matching the rule
`attestation.py` applies to token claims — and `docker run -e VAR=` is exactly
how an operator would blank a baked `ENV`, so it is tested.

Two details worth knowing:

- **`GOOGLE_APPLICATION_CREDENTIALS` being set at all is a refusal**, not a
  fallback path. `03_SECURITY_ACCESS.md` §9: key-file ADC is not a supported
  configuration anywhere in MCPForge.
- **The root check is redundant with `USER`, and stays.** A launch policy
  change, or an edit that drops the `USER` line, would otherwise silently make
  the workload root. `docker run --user 0:0` proves it fires.

The runtime credential scan sees what the workload user can read. `/root` is
mode `0700` on the base image, so a permission error there is *not* evidence of
absence — it means this process cannot tell. Those paths are skipped at runtime
and covered instead by the layer scan below, which reads the artefact rather
than the running filesystem. The two checks are complementary and the bound on
each is written into its docstring.

---

## `build.sh`

```bash
./build.sh                # build, load locally, print the digest. No push.
./build.sh --push         # the same, then push and verify the pushed digest
./build.sh --check-base   # report whether the pinned base digest has drifted
```

The default path performs **two** builds, the second fully cached:

1. Into an OCI layout at `.build/oci` with `rewrite-timestamp=true`. This is the
   canonical artefact; its manifest digest is the value printed, and it is what
   the tests inspect.
2. `--load` into the local daemon as `…/workload:local`, so the tests can
   actually run the container.

Both use `--platform linux/amd64` (Confidential Space is AMD SEV / Intel TDX on
x86-64) and `--provenance=false --sbom=false`. Build attestations would change
the digest and turn the manifest into an index; the attestation that matters
here is the hardware one, and an attestation claim carries one image digest, so
pinning an index would pin something the token never names.

`SOURCE_DATE_EPOCH` is the constant `0`, set by the script and deliberately
never read from the environment. It *was* the last commit's timestamp, which
sounds reproducible and is not: `rewrite-timestamp=true` stamps the build-created
layers and the image config with it, so a single empty commit changed the digest
and this ticket's own landing commit would have invalidated the pin it exists to
produce. `test_the_digest_does_not_depend_on_the_commit_timestamp` and
`test_an_inherited_source_date_epoch_is_ignored` fail if either half of that
stops holding. Without any epoch at all the image config would record "now" and
two builds of one tree would differ.

**What `--push` does.** It pushes to
`us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor/workload:latest`
with the same exporter settings, then reads the digest back with
`docker buildx imagetools inspect` and **aborts if it differs** from the locally
computed one. A pinned digest is only meaningful if the registry holds those
exact bytes, and a claim of reproducibility that is never checked is a claim.

It requires `gcloud auth configure-docker us-central1-docker.pkg.dev` to have
been run. It creates nothing: no repository, no service account, no IAM binding.
That is `F8-02b`.

**`--push` has not been run.** The registry is empty. Pushing is a spend
decision for the project owner and is deliberately left to them.

---

## Tests

`services/api/tests/test_workload_image.py`, 53 test functions in three groups. The count is asserted by `test_the_readme_states_the_real_number_of_tests`, because a number in prose goes stale silently — this one said 33.

**Static** — `Dockerfile`, `Dockerfile.dockerignore` and `build.sh` as text.
No Docker, no network.

**Artefact** — the real OCI layout: manifest, platform, config, environment,
instruction history, labels, user, entrypoint, installed distributions, and the
contents of every layer.

**Behavioural** — the entrypoint's refusals, run twice: as a host subprocess
with a controlled environment, and inside the real container via `docker run`.

### Build secrets, and why the Dockerfile is no longer the thing checked

This is the part of the ticket that took six review rounds, and every round
failed the same way: **a check that matched text near a property rather than at
the property.** Two rounds produced a working credential escape — a
`GEMINI_API_KEY` in the attested image's config with the whole suite green,
which is CLAUDE.md non-negotiable 2 *inside* the trust boundary.

The four measured escapes, each of which beat one or both of the two guards
that existed:

| Payload | Why it worked |
|---|---|
| a comment line ending in `\` above an `ENV` | Docker drops comment lines **before** joining continuations. Both guards joined first, so the `ENV` was swallowed into the comment. The helper returned only `['FROM python:3.12-slim-bookworm']`; the shell guard printed nothing |
| `ENV GEMINI_API_K\` + `EY=…` | Python joined continuations with a **space**, Docker with **nothing**, so the token never reassembled on the Python side |
| `env gemini_api_key=…` | Docker's verbs are case-insensitive; the shell guard's `grep` was not |
| `ENV GEMINI_KEY=…`, or a `#` in an earlier `ENV` value | the Python list held `API_KEY` and `PRIVATE_KEY` but no bare `KEY`; the shell guard's `[^#]*` stopped at the first `#` in the joined text |

Two implementations that can drift is the cause, and both fixes follow from
that.

**One rule, one list.** `dockerfile_scan.py` holds the only Dockerfile parser
and the only keyword list. `build.sh` runs it as a script before it builds;
`test_workload_image.py` imports it. It parses the way Docker parses — comment
lines removed first, continuations joined with the empty string — and refuses
outright, rather than approximating, if the file sets the `escape` parser
directive. Every payload in the table above is a parametrised case of
`test_a_measured_build_secret_escape_is_reported`, so each one is proved to go
red. The module is a build-time guard and is **not** in the image: nothing
copies it, and `Dockerfile.dockerignore` does not re-include it.

**And it is not the control.** A text scan catches only what it can spell, and
that is exactly what six rounds demonstrated. The controls read the built
artefact, which is what the digest covers:

- `test_the_image_config_declares_exactly_the_documented_environment` asserts
  `Config.Env` **exactly**, name and value, against `EXPECTED_IMAGE_ENV`. An
  environment variable that exists is in that list or the test fails — however
  the Dockerfile spelled it. Five of the ten entries come from the pinned base
  image; `GPG_KEY` is the Python release signing key's public fingerprint and
  `PYTHON_SHA256` a published source checksum, and they are listed rather than
  excused by a pattern so that nothing is excused by a pattern.
- `test_the_final_stage_ran_exactly_the_documented_instructions` asserts the
  final stage's recorded instruction history **exactly**, in order. A
  continuation, an interior comment or a lowercase verb is already resolved by
  the time BuildKit writes `created_by`. A keyword scan was tried here first
  and was wrong for this input — the description label contains "token" and the
  user-creation `RUN` writes to `/etc/passwd` — and false positives are how a
  keyword list gets shortened.

Its bounds, stated: BuildKit records only the final stage's instructions plus
the base image's, so the `deps` stage's `pip install` is not in the history, and
a `--mount=type=secret` is not recorded in `created_by` at all. Those are
covered by the installed-closure test below and by the layer scan.

### The installed closure, asserted positively

`test_the_installed_distributions_are_exactly_the_pinned_closure` reads every
`*.dist-info` under the image's `site-packages` and requires the set of
`{name: version}` to equal `requirements.txt` **in both directions**.

It replaces counting pip spellings, which cannot be done. The regex that was
here matched `uv pip`, `python -m pip` and `pip<digits>`, and review measured
four ordinary spellings it missed — `pip --no-cache-dir install` (the flag
order this Dockerfile itself uses), `pip -q install`, `pip3.12 install` and
`python -mpip install` — so a second, unpinned install passed the "exactly one
install" assertion. `$PIP install` defeats any such regex outright.

Every install writes a `.dist-info`, so a second one — chained onto the first
`RUN`, spelled through a variable, or added in a stage no test reads — shows up
as a distribution `requirements.txt` does not pin. The reverse direction
matters as much: a pin that is not installed means the hash-pinned closure is
not what the image runs.

The text-level check that remains is deliberately the weakest rule that cannot
be spelled around at that level: **any** `RUN` instruction containing the word
`install` is an install, and there must be exactly one. In an attested image
every install is a supply-chain event, `apt-get` included.

### The layer scan, and what it would catch

`test_no_layer_contains_credential_material` opens **every layer blob** of the
built image as a tar and reads its member names. Not `docker history`, not the
Dockerfile text, and not the flattened filesystem — the layers themselves,
because `RUN rm` does not remove anything: the file stays in the layer that
added it, and that layer is part of the digest the attestation covers. A
credential added in one step and deleted in the next is still inside the
attested artefact, and is exactly the case a filesystem-level check misses.

It flags a member whose basename is credential-shaped (`.env`, `.env.*`,
`.npmrc`, `.netrc`, `credentials.json`,
`application_default_credentials.json`, `id_rsa`, `id_ed25519`, …), whose
suffix is certificate- or key-shaped (`.pem`, `.crt`, `.cer`, `.key`, `.p12`,
`.pfx`, `.jks`, `.keystore`, `.ppk`), or any of whose path components is
`.ssh`, `.gnupg`, `.aws` or `.docker`.

**What it does not catch.** It matches *paths*, and reads content only to decide
whether a path match may be exempted. So it does not catch a credential in a
file with an innocuous name (`appconfig.json`), one inside a nested archive
(`secrets.tar.gz`), or one reached through a symlink whose own name is
unremarkable — all three were demonstrated by the reviewer. Those limits are
inherent to a name scan and are stated rather than papered over. What it does
guarantee: a credential-shaped **name** in any layer, including a layer whose
file a later layer deletes, is reported; and the exemption below cannot be used
to smuggle one past it.

**The one exemption**, because the base image legitimately contains 154 matches:
Debian's public CA trust store under `etc/ssl/certs/`,
`usr/share/ca-certificates/` and `usr/local/share/ca-certificates/`, plus
`usr/lib/ssl/cert.pem` and pip's vendored `certifi/cacert.pem` by exact path.
These are published root certificates with no private key, and removing them
would leave the image unable to verify anyone.

The first version of this exemption was **wrong, and dangerously so**. It
exempted on location alone, so `etc/ssl/certs/.env`, `etc/ssl/certs/id_rsa`,
`etc/ssl/certs/application_default_credentials.json` and a `.pem` holding a
private key all reported clean — the CA directory became a hiding place inside
the very control meant to find things hidden there. Four conditions now have to
hold, and the order matters:

1. the **name** is certificate-shaped (`.pem`, `.crt`, `.cer`) — checked before
   location, so `etc/ssl/certs/id_rsa` never reaches the location rule;
2. the **location** is one of the trust-store directories or exact files above;
3. a **symlink** is exempt only if its target is itself certificate-shaped and
   inside the trust store;
4. otherwise the **content** is read: a PEM certificate block must appear in
   the first 64 KiB, and a PEM private-key block must appear **nowhere in the
   member at all**. A file whose bytes could not be read is never exempted.

The two halves of condition 4 are deliberately asymmetric, and the asymmetry is
the second thing review found here. Both once used the same 64 KiB prefix, and
two real exempted members are larger than that:
`etc/ssl/certs/ca-certificates.crt` and pip's vendored `cacert.pem`. A
concatenated CA bundle is the most appendable file in the image, so appending a
key past the cap was exempted — measured on the real file. The certificate
marker is still sought in the prefix, because a PEM bundle declares itself in
its first block; the private-key marker is now sought over the whole member by
streaming, with memory bounded by the chunk size rather than the file size.

Three tests hold this, and each fails if the corresponding sentence above stops
being true:

- `test_the_only_exempted_files_are_real_public_certificates` asserts all four
  conditions against the real exempted set of the real image.
- `test_a_private_key_past_the_read_cap_is_still_caught` builds a bundle four
  times the cap with the key beyond it and requires it to be reported;
  `test_a_marker_split_across_a_stream_chunk_boundary_is_still_found` covers the
  carry that makes the streaming search correct; and
  `test_the_real_image_has_exempted_members_larger_than_the_read_cap` asserts
  the blind spot is live in this image rather than hypothetical.
- `test_a_credential_hidden_in_the_trust_store_is_still_caught` plants, in each
  exempted directory, a private key named `leaked.pem` and `leaked.crt`, an
  `id_rsa`, a `.env`, an ADC file, a service-account file, an `.aws/credentials`
  file, an unreadable `.pem`, a `.pem` that is not a certificate, and every
  key-shaped suffix — and requires **all** of them to be reported.
- `test_a_genuine_public_certificate_is_still_exempted` is its counterweight, so
  the test above cannot be satisfied by a scan that simply exempts nothing.

**The scan is proved able to fail.** `test_the_layer_scan_catches_a_planted_credential`
builds a derived image that copies a `.env` into `/opt/mcpforge`, exports it,
and requires the same function that passes above to report it. Without that, "no
offenders" and "the scan is broken" look identical. Two further self-guards: the
scan must have read more than 1,000 paths, and it must have found
`entrypoint.py` — otherwise it is looking at the wrong artefact.

The names it matches are a deliberate **second copy** of the list in
`entrypoint.py`, not an import of it. The runtime check and the artefact check
are independent controls; sharing one list would mean a single edit disabled
both. Duplication is not licence to diverge, though:
`test_the_two_credential_lists_have_not_diverged` reads `entrypoint.py`'s own
AST and fails unless this file's lists are a superset of the runtime ones. It
exists because they had already diverged — `.crt` was in the entrypoint and not
here, which made every `.crt` in every layer invisible to this scan.

### Reproducibility, and why the digest is built with a cold cache

A pinned digest is only meaningful if the same source produces it again. Three
causes had to be taken out of the digest for that to be true here — two clocks
and the build cache, which is not a clock. Each was found by a review round,
and none of them was visible to "build it twice and compare", because in every
case both builds shared the thing that varied.

**1. Source mtimes.** `COPY` preserves the source file's mtime, and
`rewrite-timestamp=true` only clamps timestamps *newer* than
`SOURCE_DATE_EPOCH` — it does not raise older ones. A fresh `git clone` writes
files at checkout time, always newer than HEAD's commit time, so they get
clamped; a working tree holding those files from before the last commit keeps
mtimes that do not. The reviewer measured exactly this: two clean clones agreed
with each other and disagreed with the working tree on one layer whose extracted
contents were byte-identical. The Dockerfile now normalises every copied mtime
to the epoch. `test_every_workload_file_has_a_normalised_mtime` asserts it
directly on the exported layers, and
`test_the_digest_does_not_depend_on_source_file_mtimes` builds from two contexts
whose mtimes are five years apart and requires one digest.

**2. The commit clock.** `SOURCE_DATE_EPOCH` was `git log -1 --pretty=%ct`,
which sounds reproducible and is not: `rewrite-timestamp=true` stamps
build-created layers and the image config with it, so a single **empty commit**
changed the digest. That is fatal for a pinned value — the commit landing this
ticket would have invalidated the digest the ticket exists to produce. It is now
the constant `0`, which is natural because the Dockerfile already normalises
workload mtimes to `@0`. `build.sh` sets it rather than reading it, so an
inherited environment value cannot put a caller's clock back in;
`test_the_digest_does_not_depend_on_the_commit_timestamp` builds a real
repository at two HEADs separated by an empty commit, with a hostile
`SOURCE_DATE_EPOCH` exported, and requires one digest.

**3. The build cache.** `build.sh` builds the canonical artefact with `--no-cache`.
This is not caution: a stale BuildKit cache produced a real wrong answer during
this ticket. A `deps` layer from before the mtime fix was reused after it, and
`build.sh` printed a digest for an image the current Dockerfile does not
produce — a digest that went into this README and would have become
`AttestationPolicy.image_digest`. A cache is a performance optimisation; the
value this script prints is a security assertion, and they no longer share a
code path. The `--load` build, whose digest is never pinned, still uses the
cache.

`test_the_readme_records_the_digest_that_is_actually_built` compares the digest
written above against the one the build produces, so this document cannot go
stale without the suite failing.

### Skipping

The artefact and behavioural groups need Docker, and on a cold cache they need
network access to fetch the pinned base. Without those they skip **with the
reason printed**. Because a skip that is green forever proves nothing —
Phase 7 and `F8-01` each had one — `MCPFORGE_IMAGE_TESTS_REQUIRED=1` turns every
such skip into a failure, and the `workload-image` CI job sets it and separately
fails if the run reports any skip at all.

---

## Not done here

- Nothing is pushed. The Artifact Registry repository is empty.
- No workload identity pool, OIDC provider, service account or IAM binding
  exists — that is `F8-02b`, and no `gcloud` command that changes state was run.
- No attestation token is obtained or verified by anything in this image, and
  none is simulated — that is `F8-02`.
- The digest above has not been confirmed against the registry.
- The Artifact Registry repository `mcpforge-executor` was created by the
  project owner before this ticket began. No `gcloud` command that changes state
  was run for `F8-02a`; the only `gcloud` calls made were read-only
  (`config get-value`, `repositories list`, `images list`).
- Confidential Space has never launched this image. It has been run under
  ordinary Docker only, which exercises the entrypoint but not the launch
  policy: the labels are asserted to be *present and correct on the image*, and
  their *enforcement* is Confidential Space's, which we have not observed.
