# Workload identity policy — `F8-02b`

What an attestation token has to say before Google's Security Token Service will
exchange it for a credential of the MCPForge workload service account, and what
that service account is then allowed to do.

This document and `setup.sh` are two halves of one statement. `setup.sh` runs
`setup_scan.py --consistency` against this file **before it plans or applies
anything** and refuses to run if a clause or a role appears in one and not the
other, in either direction. There is one clause splitter, one whitespace rule
and one comparison, in `setup_scan.py`, shared by the script and by
`services/api/tests/test_confidential_space_setup.py`. `F8-02a` lost three
review rounds to two implementations of one rule drifting apart; that is why
this is arranged the way it is.

**Status (observed read-only, 2026-09-11).** What this document declares is what
is live: the provider's condition pins `sha256:cebf7ea1…`, the attestation
bucket exists with its bucket-scoped role, the stale `76a88540…` binding was
removed, and `setup.sh --verify` reports no drift. The live verification record,
including the real attestation run, is in `README.md`.

---

## 1. The identifiers

| Item | Value |
|---|---|
| Project | `mcpforge-aa5c2` |
| Pool | `mcpforge-confidential-space` (location `global`) |
| Provider | `mcpforge-attestation` |
| Issuer | `https://confidentialcomputing.googleapis.com` |
| Workload service account | `mcpforge-workload@mcpforge-aa5c2.iam.gserviceaccount.com` |
| Workload image | `us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor/workload` |
| Pinned digest | `sha256:cebf7ea1fcb0e898142041507c3a77b7590651a2fb03f14e8f82be548ef89765` |

The issuer is the same string as `CONFIDENTIAL_SPACE_ISSUER` in
`services/api/src/mcpforge/execution/attestation.py`, and
`test_the_provider_issuer_is_the_verifiers_issuer` fails if they ever differ.
The digest is the one `build.sh` produces and `README.md` records;
`test_the_condition_pins_the_digest_the_readme_records` fails if they differ.

---

## 2. The attribute condition, clause by clause

The condition is evaluated by Google against the incoming attestation token
before any credential is issued. Every clause below is one line of the
expression `setup.sh` passes to
`gcloud iam workload-identity-pools providers create-oidc --attribute-condition`,
and the clauses are joined with `&&` — so **every** one must hold. There is no
`||` anywhere in it, and `setup_scan.py` refuses to evaluate a condition that
grows one.

<!-- BEGIN ATTRIBUTE CONDITION -->
```cel
assertion.swname == 'CONFIDENTIAL_SPACE'
assertion.submods.container.image_digest == 'sha256:cebf7ea1fcb0e898142041507c3a77b7590651a2fb03f14e8f82be548ef89765'
assertion.hwmodel in ['GCP_AMD_SEV', 'GCP_AMD_SEV_ES', 'GCP_AMD_SEV_SNP', 'GCP_INTEL_TDX']
assertion.dbgstat == 'disabled-since-boot'
'STABLE' in assertion.submods.confidential_space.support_attributes
'mcpforge-workload@mcpforge-aa5c2.iam.gserviceaccount.com' in assertion.google_service_accounts
```
<!-- END ATTRIBUTE CONDITION -->

### `assertion.swname == 'CONFIDENTIAL_SPACE'`

**Claim constrained: `swname`, the software stack that produced the token.**
Confidential Space sets it to `CONFIDENTIAL_SPACE`. Any other value means the
token came from a different attestation flavour whose other claims do not carry
the meanings assumed below — `image_digest`, in particular, is a Confidential
Space claim about the container it launched. Equality, not membership: there is
exactly one acceptable stack. Mirrors `AttestationPolicy.required_software_name`.

### `assertion.submods.container.image_digest == 'sha256:cebf…89765'`

**Claim constrained: the digest of the container image Confidential Space
actually launched.** This is the clause the whole ticket exists for. It is exact
equality against one 64-hex-character digest — the `F8-02a` image, which is the
artefact the attestation is *about*. It is deliberately **not** a prefix match,
not `startsWith`, not `matches`, not a repository name, and not a tag: a tag is
mutable and a pattern admits an image nobody reviewed. An image built from a
changed Dockerfile has a different digest and fails here, which is the intended
behaviour and the reason `setup.sh` must be re-run after a rebuild.

A wildcard in this clause would leave a working federation that attests to
nothing in particular, and it would look entirely healthy. That is the failure
mode this repository's tests target directly:
`test_the_digest_clause_is_exact_equality_against_the_pinned_digest` fails on
any operator other than `==` or any right-hand side other than the pinned
digest, and `test_a_token_with_another_image_digest_does_not_satisfy_the_condition`
fails if such a token is admitted.

### `assertion.hwmodel in ['GCP_AMD_SEV', 'GCP_AMD_SEV_ES', 'GCP_AMD_SEV_SNP', 'GCP_INTEL_TDX']`

**Claim constrained: `hwmodel`, the hardware root of trust.** These four are the
confidential platforms MCPForge accepts, and the set is exactly
`DEFAULT_ALLOWED_HARDWARE_MODELS` in `execution/attestation.py` —
`test_the_condition_allows_exactly_the_hardware_models_the_verifier_allows`
fails if the two sets ever diverge, in either direction. A membership test is
correct here where equality is correct for `swname`, because more than one
genuine confidential platform exists and MCPForge does not prefer one; what it
refuses is a machine that is not one of them.

### `assertion.dbgstat == 'disabled-since-boot'`

**Claim constrained: `dbgstat`, whether a debugger has been attached since
boot.** Anything other than `disabled-since-boot` means the memory-encryption
guarantee does not hold, so the token proves nothing about confidentiality even
though it is cryptographically valid. Mirrors
`AttestationPolicy.required_debug_status`, and
`test_a_debug_token_does_not_satisfy_the_production_condition` fails if a token
saying anything else is admitted.

### `'STABLE' in assertion.submods.confidential_space.support_attributes`

**Claim constrained: `submods.confidential_space.support_attributes`, the
Confidential Space image variant.** The debug variant of the Confidential Space
image does not carry `STABLE`. This is the second, independent way a debug
launch is refused: `dbgstat` above describes the *VM's* debug state, this one
describes the *Confidential Space image* the VM booted. Either alone would
close the case the other misses.

This clause has no counterpart in `AttestationPolicy` — the verifier does not
read `support_attributes` — so it is strictly additional narrowing at the IAM
layer and is not claimed to be enforced twice.

### `'mcpforge-workload@…' in assertion.google_service_accounts`

**Claim constrained: `google_service_accounts` — plural, and an array of
strings.** Google's token-claims reference describes it as "the validated
service accounts that are running the Confidential Space workload". It ties the
token to the workload identity we created rather than to any attested workload
in the project.

Membership, not equality, and the plural name is not a detail. An earlier
version of this document and of `setup.sh` used a singular
`assertion.google_service_account == '…'`. **No such claim exists.** A CEL
conjunction referencing an absent field errors and therefore denies, so the
condition was not permissive — it was unsatisfiable. The federation this
document describes could never have authorised a single genuine token, and the
narrowing to our workload identity was carried by no clause that would ever
evaluate. The test suite passed throughout because the token fixtures modelled
the same wrong shape: a self-consistent fiction, which is the one failure mode
a conformance check against our own fixtures cannot detect. It was found by
reading Google's documentation, not by running the tests.

`services/api/src/mcpforge/execution/attestation.py` carried the identical
mistake and was corrected with it, so `AttestationPolicy.workload_service_account`
is now checked for membership in the array. The two still mirror each other;
they are now both right.

### What the condition deliberately does not constrain

- **The audience.** Confidential Space mints the STS token with the provider's
  own resource name as `aud`, so no `--allowed-audiences` is configured and the
  default audience applies. The per-run nonce audience that
  `AttestationPolicy.audience` pins is a *different* token — the one the
  workload requests for MCPForge itself. In `F8-02` the MCPForge API issues
  that audience, receives the token through the attestation bucket and
  verifies it against its own record, once per run — that is where replay is
  refused. It is not constrained here, and saying otherwise would claim replay
  protection this federation does not deliver.
- **`iat` / `exp`.** Google validates token lifetime during the exchange, and
  `verify_attestation_token` validates it again with a bounded skew.

---

## 3. Attribute mapping

```
google.subject               = assertion.sub
attribute.image_digest       = assertion.submods.container.image_digest
attribute.hwmodel            = assertion.hwmodel
attribute.dbgstat            = assertion.dbgstat
attribute.swname             = assertion.swname
```

`attribute.image_digest` exists because the principal set in §5 selects on it.
The other three are mapped so that an operator reading an IAM policy or a log
can see which hardware and which debug state a principal presented, without
having to reconstruct it from the raw token.

---

## 4. Roles held by the workload service account

These, and nothing else. `setup.sh --verify` reports any role the account holds
beyond this list and exits non-zero, because least privilege is a claim about
what is **absent** and a list of grants alone cannot support it.

<!-- BEGIN PROJECT ROLES -->
```
roles/confidentialcomputing.workloadUser
roles/artifactregistry.reader
```
<!-- END PROJECT ROLES -->

| Role | Why the workload cannot do its job without it |
|---|---|
| `roles/confidentialcomputing.workloadUser` | The Confidential Space VM presents this identity when it asks the Confidential Computing API for an attestation token. Without it the workload obtains no token, and `verify_attestation_token` has nothing to verify — so there is no `HARDWARE_ATTESTED` path at all. |
| `roles/artifactregistry.reader` | The VM pulls the workload image from `us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor`. Read on Artifact Registry, not write: nothing running inside the TEE may publish an image, least of all the image it is itself attested as. |

### Bucket roles — the attestation bucket only (F8-02)

The workload writes its raw attestation token to
`gs://mcpforge-aa5c2-attestation/attestation/<run id>.jwt`, and the MCPForge API
reads it and verifies it. This role is granted **on that one bucket**, never on
the project, and `setup.sh --verify` reports any other role the account holds
there.

<!-- BEGIN BUCKET ROLES -->
```
roles/storage.objectCreator
```
<!-- END BUCKET ROLES -->

| Role | Why, and why nothing wider |
|---|---|
| `roles/storage.objectCreator` on `gs://mcpforge-aa5c2-attestation` | The narrowest predefined role that can create an object: `storage.objects.create` and nothing else of substance. It cannot read, list, overwrite or delete objects, so a compromised workload can deliver a token for its run and cannot read, replace or erase anyone else's. Overwrite is refused twice — the role lacks delete, and the workload uploads with `ifGenerationMatch=0`. |

The bucket itself: `us-central1`, uniform bucket-level access, public access
prevention enforced, and a lifecycle rule deleting every object one day after
creation (`attestation-lifecycle.json`). The token is signed by Google, so the
bucket is transport, not a trust anchor: whoever could write to it could at
most deliver a token that fails verification. The API reads with the owner's
Application Default Credentials; no service-account key exists for either side.

`roles/logging.logWriter` is **not** granted, and the reason is a decision made
one file away. The image sets `tee.launch_policy.log_redirect=never`
(`infra/confidential-space/Dockerfile`), so Confidential Space does not write the
workload's stdout or stderr to the operator's Cloud Logging at all. Granting a
role for logs that are deliberately never sent would be privilege the workload
cannot use, and it would quietly contradict the launch policy. An earlier draft
of this document granted it and justified it by saying Confidential Space writes
the workload's output to Cloud Logging — which is false while `log_redirect` is
`never`. If that label is ever relaxed to `debugonly`, the debug image has a
different digest and fails the attribute condition below, so the role would have
to be reconsidered together with the condition rather than added on its own.

**Other roles deliberately not granted**, each of which would be easy to add and
wrong: any `roles/storage.*` at the project level, and on the attestation bucket
anything beyond `objectCreator` — not `objectViewer` (the workload has no reason
to read a token back), not `objectUser` or `objectAdmin` (they can delete and
overwrite), not `legacyBucketWriter`; any
`roles/artifactregistry.writer` (see above); `roles/iam.serviceAccountTokenCreator`
(the workload impersonates nobody; federation flows the other way);
`roles/editor` or any other basic role; and anything on Firestore — the workload
analyses a repository and returns a result, and the service persists it.

**The account has no keys.** No service-account key is created by `setup.sh`,
none exists, and none is a supported configuration anywhere in MCPForge
(`03_SECURITY_ACCESS.md` §9). Workload identity federation is the mechanism that
makes a key unnecessary; creating one would reintroduce exactly the long-lived
credential the TEE boundary exists to avoid.
`test_the_setup_script_never_creates_a_service_account_key` fails if that ever
changes.

---

## 5. The pool → service account binding

```
member = principalSet://iam.googleapis.com/projects/<PROJECT_NUMBER>/locations/global/
         workloadIdentityPools/mcpforge-confidential-space/
         attribute.image_digest/sha256:cebf7ea1fcb0e898142041507c3a77b7590651a2fb03f14e8f82be548ef89765
role   = roles/iam.workloadIdentityUser
```

Granted **on the service account**, not on the project. The member is not the
whole pool: it is the subset of the pool's principals whose mapped
`attribute.image_digest` is the pinned digest. So the digest is enforced twice
and independently — once by the provider's attribute condition, which decides
whether a token is admitted to the pool at all, and once by this binding, which
decides which admitted principals may act as this service account. `setup.sh`
builds both from the single `IMAGE_DIGEST` variable, so they cannot disagree,
and `test_every_runtime_mention_of_the_digest_is_the_pinned_digest` checks every
occurrence in the captured gcloud invocations rather than in the file's text.

`<PROJECT_NUMBER>` is read at run time with `gcloud projects describe`; it is not
hardcoded, and it is a project number rather than a project id because IAM
principal sets require the number.

---

## 6. What this document does not claim

- **This document's declaration is what is live** (`setup.sh --verify`,
  2026-09-11, no drift).
- **The tests do not evaluate CEL the way Google does.** `setup_scan.py` holds a
  small evaluator over the subset this condition uses — `&&`, `==`, `in`,
  dotted paths, string and list literals — and refuses anything outside it. It
  proves that a debug token, a wrong-digest token, a non-confidential-hardware
  token and a wrong-service-account token all fail *this expression*. It does
  not prove that Google IAM accepts the expression, evaluates it identically, or
  that Confidential Space emits the claim names assumed here. Of those four,
  the claim names are now confirmed — the verified run's real token carried
  exactly these claims and values — and Google accepted the expression when
  `setup.sh --apply` wrote it. Whether IAM *evaluates* it identically was not
  exercised: that run delivered its token with the VM's attached service
  account, not through federation.
- **A correct condition is not an attestation.** `F8-02` — obtaining a real
  token, verifying it in the API and reporting `HARDWARE_ATTESTED` — was
  demonstrated on 2026-09-11 (run `cs-20260910-234427-b3b40d`), and it rests on the
  relying party's verification, not on this condition.
