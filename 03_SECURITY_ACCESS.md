# MCPForge — Security & Access Model

**Status:** Phase 0 baseline. This document is binding. Code that contradicts it is a defect, not a variation.

MCPForge reads proprietary source code and produces code that AI agents will be able to invoke. Both halves are security-critical, so security is a product requirement here, not a hardening pass at the end.

---

## 1. Threat model

| # | Threat | Control |
|---|---|---|
| T1 | User source code leaks to Gemini or logs | Path policy + secret filter run before context construction (§4); prompts never log file bodies |
| T2 | A repository secret is embedded in a prompt or a generated file | Quarantine before indexing; generated-patch scan before PR (§4.4) |
| T3 | MCPForge writes to a user's default branch | Branch-only writer, repository-id assertion, no force push (§6) |
| T4 | Prompt injection from repository content or from an agent caller drives a privileged action | Model output is data, never authorization (§7); all gates are deterministic |
| T5 | Generated WebMCP tool exposes a destructive capability without approval | Risk classification + mandatory approval for WRITE/DESTRUCTIVE, enforced server-side (§8) |
| T6 | Analysis job escapes its sandbox or exfiltrates over the network | SecureExecutionProvider: path jail, non-root, no outbound network for analysis (§3) |
| T7 | False trust signalling — user believes work ran in a TEE when it did not | Trust level enum, set only by verified attestation (§2) |
| T8 | Credentials exposed to the browser | Hard tier boundary; only `NEXT_PUBLIC_*` reaches the client (§9) |
| T9 | Over-broad GitHub access | GitHub App, per-repository installation, short-lived tokens (§6) |
| T10 | An AI agent calling MCPForge's own WebMCP tools performs actions the human never approved | Same approval records as the UI; no agent-only path (§8.3) |

## 2. Confidential execution

**Production target:** Google Confidential Space, or an equivalent attested confidential-computing boundary, for repository analysis of private code.

**Phase 0–7 reality:** `DevelopmentSecureExecutor` — process isolation, ephemeral workspace, path jail, no outbound network for analysis commands, resource limits. This is real isolation, and it is *not* hardware-backed.

The distinction is represented as an enum, never a boolean:

```python
class TrustLevel(StrEnum):
    DEVELOPMENT_ISOLATION = "DEVELOPMENT_ISOLATION"
    HARDWARE_ATTESTED     = "HARDWARE_ATTESTED"
```

Rules:
- `HARDWARE_ATTESTED` may be assigned **only** by code that has fetched and cryptographically verified an attestation token against the expected workload identity and image digest.
- There is no configuration flag, no environment variable, and no test fixture that can set `HARDWARE_ATTESTED` without that verification path.
- The UI renders the enum. The strings shown to the user are **"Development Isolation"** and **"Hardware-backed Confidential Execution Verified"** — the second string exists in exactly one branch of one component, guarded by the enum.
- Writing "TEE VERIFIED", "attested", or a green shield for `DEVELOPMENT_ISOLATION` is a `FAIL` at review, unconditionally.

**Where that verification lives (F8-01).** `services/api/src/mcpforge/execution/attestation.py` defines the trust enum, the evidence record, the failure taxonomy, an `AttestationPolicy`, and `verify_attestation_token` — the single function permitted to produce `HARDWARE_ATTESTED`. `AttestationEvidence` carries the same single-producer rule and is constructed in exactly one place, because holding evidence is what raises the trust level; both rules are swept for over every backend module by test. It checks, in order: a non-empty token; an asymmetric algorithm from a fixed allowlist, refused before the signing key is even resolved; the signature against a resolved public key; issuer; audience; expiry and not-before with a bounded clock skew; required registered claims; a single audience that is exactly ours — a token addressed to us *and* to somebody else is refused, because the audience is a per-run nonce; the expected workload identity being **present in** `google_service_accounts`, which is plural and an array of strings (an earlier version compared against a singular `google_service_account`, a claim no genuine Confidential Space token carries — it would have rejected every real token, and `F8-02b`'s attribute condition mirrored the same mistake; both passed their tests because the fixtures modelled the same wrong shape, and it was found by reading Google's documentation rather than by running anything); container image digest, matched exactly against a canonical `sha256:<64 lowercase hex>` with no case folding and no prefix match; hardware model; software stack; and debug status.

String claims are compared **without normalisation** — not stripped, not case-folded. A padded `"  sha256:<hex>  "`, `" GCP_AMD_SEV "` or a padded service account is a mismatch, not a match. The one normalisation that remains is that a whitespace-only claim counts as absent, and it is pinned by its own test. The *policy* side is normalised, because a developer writes it.

**Every** failure returns an outcome whose trust level is `DEVELOPMENT_ISOLATION` with no evidence and a recorded reason — there is no partial credit and no fail-open on a network error. `verify_attestation_token` raises nothing at all: every anticipated failure becomes an outcome with a **named** reason, and a fail-closed backstop yields `VERIFICATION_ERROR` for anything unanticipated. An `AttestationOutcome` cannot be constructed with a raised trust level and no evidence.

Four real escapes were found across three review rounds, and all four are now handled by name: an out-of-range `exp` raising `OverflowError` from the datetime conversion; a signing key PyJWT cannot parse raising `InvalidKeyError`, which is a *sibling* of `InvalidTokenError` rather than a subclass; a non-finite `exp`/`iat` raising `OverflowError` from `int()` inside PyJWT's own claim validation; and a non-numeric `nbf` raising `TypeError` from that same conversion — on a claim this module never names, which PyJWT validates anyway. A fifth escape came from the key side: a key source returning an RSA *private* key, which `prepare_key` accepts and `verify` then does not exist on. A prepared key without a `verify` method is now refused as `UNRESOLVED_SIGNING_KEY`.

The tests therefore assert not merely that verification returns an outcome, but that the failure is **not** `VERIFICATION_ERROR`. **What is demonstrated, and its bound:** no input the test matrix can construct reaches the backstop — every RFC 7519 registered claim and every claim the module reads, each crossed with a hostile-value list; every header parameter, since the header is read before the signature is verified; and every unusable key shape, each against a genuinely misconfigured key source. That is a bound on the enumeration, not a proof of unreachability, and the enumeration is derived from the module's own AST rather than from recall: `nbf` was missed twice because a list was written from memory, and `test_every_claim_the_module_reads_has_hostile_shapes` now fails if a claim the code reads has no hostile shape.

An under-specified policy — no audience, no expected digest, an empty allowed-hardware set — raises at construction rather than verifying permissively.

**The image is the artefact the attestation is about (F8-02a).** A hardware
attestation states that one exact image ran on genuine confidential hardware; it
says nothing about whether that image should be trusted. Everything baked in
therefore sits inside the trust boundary. `infra/confidential-space/` builds a
minimal image holding the secure executor only, from a digest-pinned base, with a
hash-pinned dependency closure, no build secret, no `.env` and no ADC file,
running as a fixed non-root uid under an exec-form entrypoint that **refuses to
start** when required configuration is absent rather than substituting a default.
`tee.launch_policy.allow_cmd_override` is `false`, so an operator cannot keep the
attested digest while running something else inside it, and `allow_env_override`
is a two-name allowlist.

**"No build secret" is asserted against the image, not against the Dockerfile.**
Six review rounds on this ticket failed the same way — a check matching text
near a property rather than at the property — and two of them produced a working
escape: a `GEMINI_API_KEY` in the attested image's config with the whole suite
green, which is a credential inside the trust boundary. A comment line ending in
`\` deleted the following `ENV` from both guards while Docker executed it; a
continuation split mid-token was reassembled by Docker and not by Python, whose
join used a space; a lowercase `env` beat a case-sensitive `grep`; and a bare
`GEMINI_KEY=` beat a keyword list holding only `API_KEY` and `PRIVATE_KEY`. The
cause in every case was two implementations of one rule that could drift.
`infra/confidential-space/dockerfile_scan.py` is now the only parser and the only
keyword list, shared by `build.sh` and the test suite, parsing in Docker's own
order and refusing rather than approximating an `escape` parser directive; each
measured payload is a parametrised case of
`test_a_measured_build_secret_escape_is_reported`. It is **defence in depth**.
The controls read the artefact the digest actually covers:
`test_the_image_config_declares_exactly_the_documented_environment` asserts
`Config.Env` exactly, name and value, so an environment variable that exists is
documented or the test fails however it was spelled; and
`test_the_final_stage_ran_exactly_the_documented_instructions` asserts the final
stage's recorded instruction history exactly. A keyword scan of that history was
tried and rejected — the description label contains "token" and the
user-creation `RUN` writes to `/etc/passwd`, and false positives are how a
keyword list gets shortened. Bound: BuildKit records only the final stage's
instructions plus the base image's, and does not record `--mount=type=secret` in
`created_by` at all; the builder stage and secret mounts are covered by the layer
scan and by the text scan above.

**The dependency closure is asserted positively.** Counting install spellings
cannot be made correct: the regex that did it missed `pip --no-cache-dir
install` — the flag order this Dockerfile uses — `pip -q install`, `pip3.12
install` and `python -mpip install`, so a second unpinned install passed an
"exactly one install" assertion, and `$PIP install` defeats any such regex.
`test_the_installed_distributions_are_exactly_the_pinned_closure` instead reads
every `.dist-info` in the image's `site-packages` and requires the set to equal
`requirements.txt` in both directions, which holds however the install was
spelled.

The absence of credential material is a **T7 control, not hygiene**, and is
tested as one: `test_no_layer_contains_credential_material` opens every layer
blob of the built image and reads its tar members, because a file deleted in a
later layer is still present in the layer that added it and is still covered by
the digest — verified in review with a planted `id_rsa` removed by a later
`RUN`. The scan is proved able to fail by a companion test that builds a derived
image containing a planted `.env` and requires the same function to report it.

**What is demonstrated, and its bound.** Like the AST sweeps above, this is a
name-based check: it matches *paths*, and reads content only to decide whether a
path match may be exempted. It therefore catches a credential-shaped name in any
layer, including one a later layer deletes, and does **not** catch a credential
renamed to something innocuous, embedded in a nested archive, or reached through
a symlink whose own name is unremarkable. All three were demonstrated by the
reviewer and are inherent to a name scan.

Its one exemption is the base image's published CA trust store, and the first
version of that exemption was a hole rather than a caveat: exempting on location
alone made `etc/ssl/certs/` a hiding place, so an ADC file, a `.env`, an `id_rsa`
and a `.pem` containing a private key all reported clean inside the control meant
to find exactly those. A member is now exempt only if its name is
certificate-shaped **and** its location is a trust-store path **and**, for a
regular file, a PEM certificate block appears in its first 64 KiB and a PEM
private-key block appears **nowhere in it at all** — an unreadable file is never
exempted, and a symlink is exempt only if its target satisfies the same rules.
That asymmetry is not an implementation detail: both checks once used the same
64 KiB prefix, and two genuinely exempted members are larger than it, so a key
appended past the cap to a concatenated CA bundle was exempted. The private-key
search is now streamed over the whole member. `test_the_only_exempted_files_are_real_public_certificates`
asserts all of that on the real image, and
`test_a_credential_hidden_in_the_trust_store_is_still_caught` plants each of the
above in each exempted directory and fails unless every one is reported.

**Reproducibility is part of the control, not an aside.** A pinned digest that a
rebuild cannot reproduce cannot distinguish a legitimate rebuild from a
substituted image. **Three** causes were found across three review rounds, and
none of them was visible to "build it twice and compare", because in each case
both builds shared the thing that varied:

1. **Source mtimes.** `COPY` preserves them and `rewrite-timestamp` only clamps
   timestamps newer than `SOURCE_DATE_EPOCH`, so a fresh clone and a long-lived
   working tree disagreed on a layer whose contents were byte-identical. The
   Dockerfile now normalises every copied mtime to `@0`
   (`test_every_workload_file_has_a_normalised_mtime`,
   `test_the_digest_does_not_depend_on_source_file_mtimes`).
2. **The commit clock.** `SOURCE_DATE_EPOCH` was `git log -1 --pretty=%ct`, so a
   single **empty commit** changed the digest — meaning the commit that lands
   this work would have invalidated the digest it produces. It is now the
   constant `0`, and `build.sh` **sets** it rather than reading it, so an
   inherited environment value cannot put a caller's clock back in
   (`test_the_digest_does_not_depend_on_the_commit_timestamp`, which builds a
   real repository at two HEADs, and `test_an_inherited_source_date_epoch_is_ignored`,
   which is needed separately because the first test passes if the script
   honours an inherited value — both of its builds would inherit the same one).
3. **The build cache.** A stale BuildKit cache made `build.sh` print a digest
   for an image the current Dockerfile does not produce, and that digest reached
   the README. The canonical build now runs with a cold cache; a cache is a
   performance optimisation and the printed digest is a security assertion, so
   they no longer share a code path
   (`test_every_pinnable_build_runs_with_a_cold_cache`, which asserts
   `--no-cache` on both builds whose digest can be pinned). This cause is not
   independent of the other two: with the cache live, the tests for causes 1
   and 2 read a pre-fix exported layer and pass over the defect they exist to
   catch, so the cold cache is what makes those assertions mean anything.

The recorded digest in the README is itself asserted against the built one
(`test_the_readme_records_the_digest_that_is_actually_built`).

**Live state (observed read-only, 2026-09-11).** The owner pushed
`sha256:76a88540…` on 2026-09-10T18:56 and applied the workload identity setup
at that digest: the `mcpforge-attestation` provider is live and its condition
pins `76a88540…`. The current tree's image has a different digest, which is
neither pushed nor pinned, and the attestation bucket does not exist yet. The
image has never been launched by Confidential Space; the launch-policy labels
are asserted on the image and their enforcement has not been observed. Blocker
B-04 stands.

> **Superseded on 2026-09-11.** The paragraph above is the state before the
> real run and is kept as written. The owner then pushed `sha256:cebf7ea1…`,
> repinned the provider, created the bucket and removed the stale binding. Run
> `cs-20260910-234427-b3b40d` booted the production image on AMD SEV, and the API
> verified its token to `HARDWARE_ATTESTED`; the launcher's own log showed the
> launch policy parsed and applied — as far as the launcher reports; no
> forbidden override was attempted, so a refusal was not observed directly. B-04 is closed and `F8-02` is `DONE`.

**Who verifies (F8-02).** Attestation means
something only to a relying party **outside** the TEE: a workload that checks
its own token proves nothing, because a malicious image would simply report
success. So the workload verifies nothing. The MCPForge API is the relying
party:

1. The API issues each run — a run id and a random audience, the nonce — and
   records it pending. The verifier chose the nonce, which is what makes replay
   protection mean anything. `launch.sh` can only launch a run the API issued
   (`test_a_run_the_relying_party_did_not_issue_cannot_be_launched`).
2. The workload requests a token from the Confidential Space launcher only
   (`POST /v1/token` on `/run/container_launcher/teeserver.sock`) with that
   audience, and writes the raw token to the private object
   `gs://mcpforge-aa5c2-attestation/attestation/<run id>.jwt`, create-only.
   Every launcher failure — missing, refused, hung up, timed out, non-200, over
   64 KiB, empty, or not exactly one compact JWS — yields no token. The
   workload's storage credential comes from the metadata server in one module,
   `token_delivery.py`, kept apart from token retrieval; the metadata identity
   endpoint, a VM identity rather than an attestation, is used nowhere
   (`test_the_metadata_server_is_reached_only_by_the_delivery_transport`).
3. The API fetches the object with its own ADC and verifies it with
   `verify_attestation_token` against the audience **it** issued, the image
   digest from **its own** configuration (`CONFIDENTIAL_SPACE_IMAGE_DIGEST`,
   never an operator value), the workload service account, and Google's keys.
   The run is consumed atomically on first read, so a replay — sequential or
   concurrent — is refused (`test_a_run_verifies_once_and_a_replay_is_refused`,
   `test_two_concurrent_verifiers_cannot_both_consume_a_run`). Every refusal —
   an unissued, consumed or expired run, a missing, unreadable or malformed
   object, another run's audience, a digest other than the API's, another
   workload identity, an expired or debug token — leaves the API and the trust
   panel at `DEVELOPMENT_ISOLATION` (`test_every_refusal_leaves_the_api_and_panel_unattested`,
   over a matrix derived from the failure enum). Only a successful
   verification raises the executor's level, through F8-01's single producer.
4. The executor runs no job even then: the attested workload has no job
   runner, and running a job on the API host under an attested label would
   claim a boundary the job was never inside.

**The bucket is transport, not a trust anchor.** The token is signed by Google,
so whoever could write the object could at most deliver a token that fails
verification. It is still private: uniform bucket-level access, public access
prevention enforced, objects deleted after a day, and the workload holds only
bucket-scoped `roles/storage.objectCreator` — it cannot read, overwrite or
delete (`infra/confidential-space/policy.md`).

**The token appears in no log on either side.** Only a 16-character SHA-256
prefix and the length are recorded. `test_the_token_never_appears_in_any_output`
plants recognisable tokens and a recognisable access token and searches
structlog events (before any redaction), stdlib logging, fd-level stdout and
stderr, exception messages, `repr`s, the entrypoint's JSON and the API's
responses, workload side and API side, success and failure.
`log_redirect=never` is unchanged.

**What remains unproven.** The tests run every production function on the path
against local stand-ins for the launcher, the metadata server and Cloud
Storage, with F8-01's test key. That is not attestation: `F8-02` completes only
when a real Confidential Space run produces a token that the API verifies
against its own pinned digest and issued nonce.

The "exactly one producer" rule is enforced by an AST sweep over every backend module, matching the attribute, the bare name and the string literal. Like the approval sweep in §6 of `02_ARCHITECTURE.md`, it matches on names: it catches straightforwardly-written code and does not defeat deliberate indirection such as `getattr(TrustLevel, name)`. The guarantee is that sweep **plus** the behavioural tests that every rejection path returns `DEVELOPMENT_ISOLATION`.

**The UI half of T7 (F8-03).** The trust panel renders the trust level as an
enum read from `/api/sessions/{id}/trust`, never a boolean and never a
client-side default. The verified wording — "Hardware-backed Confidential
Execution Verified" — exists in exactly one component branch,
`apps/web/src/components/trust/secure-execution-row.tsx`, guarded by the single
comparison against the attested value that exists anywhere in `apps/web/src`.
Both facts are asserted against the TypeScript AST by
`apps/web/tests/trust-verified-branch.test.ts`, and
`apps/web/tests/trust-panel.test.tsx` asserts that development isolation renders
with the explicit "Not hardware-attested" line and with no success tone or tick
anywhere in the panel. Since nothing in the product obtains an attestation
token, the panel today always renders the unattested state; the verified branch
is unreachable in the running product rather than merely unused.

The quarantine row shows paths and never contents — quarantined files are never
opened, so no content exists to show — and shows an absent count rather than
zero until an analysis has actually run.

## 3. Sandbox rules for repository jobs

- Ephemeral workspace per run, destroyed on completion and on failure.
- Non-root execution.
- Filesystem access confined to the workspace path (path jail; symlinks resolved and rejected if they escape).
- No outbound network from analysis and validation commands. The clone step is the one networked operation and it targets only the bound repository host.
- CPU, memory, wall-clock and output-size limits; exceeded limits terminate the job with an explicit error.
- No arbitrary shell execution outside the sandbox. Commands are constructed from an allowlist with argument arrays — never string interpolation into a shell.

## 4. Secret protection

### 4.1 Ordering

Filtering happens **before** model context construction, before indexing, and before anything is persisted. There is no code path in which an unfiltered file body reaches a prompt builder.

### 4.2 Path-level exclusions (quarantine, never read)

```
.env, .env.*, *.pem, *.key, *.p8, *.p12, *.pfx, *.keystore, *.jks,
id_rsa*, id_dsa*, id_ecdsa*, id_ed25519*, *.crt, *.cer,
**/secrets/**, **/credentials/**, service-account*.json,
gcp-*.json, firebase-adminsdk-*.json, *.tfvars, .npmrc, .netrc,
.git-credentials, **/.aws/**, **/.ssh/**
```

### 4.3 Content-level scanning

Every file that survives path filtering is scanned before use for known credential formats — provider API key prefixes, PEM blocks, JWTs, connection strings with inline passwords, and high-entropy assignments to variables named like secrets. A hit quarantines the file and records a finding. Redaction of a partial match is not used for prompt safety: a file with a detected secret is excluded, not scrubbed and sent.

### 4.4 Outbound scan on generated content

The generated patch is scanned with the same detectors before the security review and again before PR creation. A hit blocks the PR.

### 4.5 Non-goals stated honestly

This is defence in depth, not a proof. A secret that looks like ordinary source text can survive filtering. The product says so in the Trust Panel rather than implying a guarantee.

## 5. Read-only by default

- A connected project begins in `READ_ONLY`.
- MCPForge cannot alter user code in this mode. There is no write code path reachable from a `READ_ONLY` project.
- Elevation to `WRITE_PR` requires: an explanation shown to the user of what will be written and where, an explicit user action, and a persisted record of who elevated and when.
- `WRITE_PR` permits exactly: create a branch, commit to that branch, open a PR. Nothing else.
- Every repository operation asserts the target repository id equals the project's bound repository id.
- A **demo project** (the bundled fixture application, `01_PRD.md` §7) has **no bound repository id**. It is therefore permanently ineligible for `WRITE_PR` and can reach no write path — the bound-id assertion above can never pass for it. This is enforced explicitly as well as structurally: elevation rejects a project without a bound repository id, and the PR writer refuses one. A demo project exists to exercise analysis, generation and validation; it never produces a pull request.

## 6. GitHub access

- GitHub App with per-repository installation. Never request account-wide repository access.
- The App private key is backend-only, loaded from the environment, never logged.
- Installation tokens are minted per operation and short-lived; they are never persisted.
- Authentication is **provisional and replaceable** (`02_ARCHITECTURE.md` §3.2). Whichever identity provider is in use, the same rules bind: the backend verifies the token itself on every authenticated request, a client-supplied identity is never trusted, and only a verified `subject` is used for ownership checks. Changing provider changes one adapter and no security control.
- MCPForge user authentication is **separate** from GitHub repository authorization. Signing into MCPForge with GitHub grants identity, not repository access. Repository access requires the App installation flow. These are distinct records in the store and distinct checks in code.
- Branch naming: `mcpforge/webmcp-<project-or-workflow-slug>`.
- Never push to the default or any protected branch. Never force push. Never rewrite history. Never delete branches the user created.

## 7. Model output is never authorization

This is the rule most likely to be violated accidentally, so it is stated concretely.

- Every model response is parsed and validated against a Pydantic schema before any use. A validation failure is an error, not a warning.
- No branch in the codebase may read a boolean, a status, or an approval from model output and use it to permit a state transition. Approval comes from an `Approval` record in the store.
- The Human Approval Agent may map "yes, go ahead" to a *proposed* decision. Committing that decision is a deterministic function that requires an authenticated user id.
- The Security Reviewer agent's `PASS` is advisory input to a deterministic gate that also applies our own policy checks. An agent `PASS` cannot clear a policy violation found by code.
- Repository content is untrusted input. Instructions found inside analyzed source code are data. Prompts state this, and — more importantly — the architecture ensures a successful injection still cannot cross a gate, because gates do not read model output.

## 8. Generated-tool safety

### 8.1 Risk classification

Every proposed tool carries a risk class assigned by the Workflow Architect and **re-checked deterministically** against the mapped function's effects:

| Class | Meaning | Approval |
|---|---|---|
| `READ` | No state change | Not required |
| `WRITE` | Creates or modifies state | Required |
| `DESTRUCTIVE` | Deletes, cancels, charges, or is irreversible | Required, with explicit confirmation text |

If the agent's classification and the deterministic check disagree, the stricter one wins and the discrepancy is surfaced as a finding.

### 8.2 Generated code requirements

Generated tools must: validate input against their declared schema before executing; call existing application logic rather than reimplementing it; never accept a raw identifier that bypasses the application's own authorization; never take a parameter that selects a table, endpoint, path, or user id arbitrarily; return structured errors rather than raw exception text; and register with an abort signal so lifecycle teardown is clean.

Generated tools must not expose: authentication bypass, arbitrary redirects, raw SQL or query fragments, file path parameters, admin operations, or bulk destructive operations.

### 8.3 MCPForge's own tools

MCPForge's WebMCP surface follows the same rules it enforces on others. Read tools are open; anything mutating creates the same `Approval` record and returns "awaiting human approval". An agent cannot approve on the human's behalf, and there is no agent-only path around a gate.

## 9. Credential and tier boundaries

- Gemini API key: backend only. Never in a `NEXT_PUBLIC_*` variable, never proxied in a way that lets a client choose arbitrary prompts without our system instruction.
- GitHub App private key: backend only.
- **No service-account key files.** Organization policy blocks their creation, and the architecture does not want them. Server-side Google credentials come from Application Default Credentials — `gcloud auth application-default login` in development, workload identity in production. A downloaded key file is never created, never committed, and never a supported configuration; `.gitignore` blocks the common filenames regardless.
- The local ADC file (`~/.config/gcloud/application_default_credentials.json`) lives outside the repository and is never copied into it, into a container image, into a log, or into a prompt.
- **Authentication needs no credentials at all.** Firebase ID tokens are verified against Google's public JWKS by signature, issuer, audience and expiry. The backend imports no Firebase SDK. The Firebase **web** config is public by design and is the only Firebase material anywhere, and it lives in the client.
- `.env` is never committed. `.env.example` contains variable names and non-secret defaults only.
- Backend verifies the Firebase ID token on every authenticated request; the client's claim of identity is never trusted.
- Logs redact tokens, keys and file bodies. Prompt/response retention for debugging is server-side, access-controlled, and excludes quarantined content.

## 10. Banned dependencies for V1

Unless a specific, written justification is added to this document:

- LangChain, LlamaIndex, CrewAI, AutoGen, or any heavy agent-orchestration framework
- `google-generativeai` (obsolete Gemini SDK) — use `google-genai`
- Arbitrary code-execution / `eval`-style libraries
- Any dependency that transmits source code to a third party
- Telemetry/analytics SDKs not required by a shipped feature
- Any "WebMCP shim" or polyfill presented as the official standard

## 11. Security review checklist (used at every gate)

1. No secret reachable by a prompt, a log, or the client bundle.
2. No write path to a default or protected branch; no force push.
3. Trust level correct and not overstated anywhere in code or UI.
4. Every state transition legal, persisted, and gated by a real `Approval` where required.
5. Every model output schema-validated before use.
6. No authorization decision derived from model output.
7. Generated tools correctly risk-classified and approval-gated.
8. Sandbox limits present on every executed command; no shell string interpolation.
9. No new banned dependency.
10. No mock or stub presented as real capability.
