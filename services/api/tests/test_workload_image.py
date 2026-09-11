"""The Confidential Space workload image — F8-02a.

The image is the attested artefact. `AttestationPolicy.image_digest` pins its
digest, so whatever is inside it is inside the trust boundary and is vouched for
by the hardware attestation. That makes
`test_no_layer_contains_credential_material` a **T7 control**, not hygiene: a
`.env` or an ADC file in any layer would be a credential the TEE attests to.

Three groups of tests live here:

1. **Static** — assertions about `Dockerfile`, `Dockerfile.dockerignore` and
   `build.sh` as text. No Docker, no network, always run.
2. **Artefact** — assertions about the real OCI layout `build.sh` produces:
   its config, its labels, and the contents of every one of its layers.
3. **Behavioural** — the entrypoint's refusals, run twice: once as a plain
   subprocess on the host, and once inside the real container.

**On skipping.** Groups 2 and 3 need a working Docker daemon and, on a cold
cache, network access to fetch the pinned base image. When that is unavailable
they skip with the reason printed. A skip that is green forever proves nothing —
Phase 7 and F8-01 both had one — so setting `MCPFORGE_IMAGE_TESTS_REQUIRED=1`
turns every one of those skips into a failure, and CI sets it. Locally it skips
so a developer with no Docker is not shown a red bar for something they did not
break.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import IO, Any, NamedTuple

import jwt
import pytest

import tests.test_confidential_space as launcher_stub
from tests.launch_plan import load_entrypoint, run_launch_plan

REPO_ROOT = Path(__file__).resolve().parents[3]
INFRA_DIR = REPO_ROOT / "infra" / "confidential-space"
DOCKERFILE = INFRA_DIR / "Dockerfile"
DOCKERIGNORE = INFRA_DIR / "Dockerfile.dockerignore"
BUILD_SCRIPT = INFRA_DIR / "build.sh"
ENTRYPOINT = INFRA_DIR / "entrypoint.py"
README = INFRA_DIR / "README.md"
OCI_LAYOUT = INFRA_DIR / ".build" / "oci"

LOCAL_TAG = "us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor/workload:local"

#: The exit codes `entrypoint.py` documents. Duplicated here on purpose: this
#: file is the independent check, and importing the constants would mean a
#: single edit could move both the behaviour and the expectation together.
EXIT_OK = 0
EXIT_CONFIG_MISSING = 10
EXIT_CONFIG_INVALID = 11
EXIT_CREDENTIAL_PRESENT = 13
EXIT_WORKSPACE_UNUSABLE = 14
EXIT_ATTESTATION_UNAVAILABLE = 16
EXIT_DELIVERY_FAILED = 17

#: A configuration that satisfies every requirement, so a test can remove
#: exactly one thing and attribute the refusal to that removal.
GOOD_CONFIG: dict[str, str] = {
    "MCPFORGE_RUN_ID": "run-f8-02a-0001",
    "MCPFORGE_ATTESTATION_AUDIENCE": "1f0c7d2a9b4e6f81c3a5",
}

#: Credential-shaped names, for the layer scan. This list is deliberately a
#: second copy of the one in `entrypoint.py` rather than an import of it. The
#: runtime check and the artefact check are independent controls; sharing a list
#: would mean one careless edit disabled both at once.
CREDENTIAL_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".envrc",
        ".npmrc",
        ".netrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "application_default_credentials.json",
        "service-account.json",
        "serviceaccount.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "gcloud.json",
        "adc.json",
    }
)

#: `.crt` is here because `entrypoint.py` has it, and
#: `test_the_two_credential_lists_have_not_diverged` fails if this list is not a
#: superset of the runtime one. Round 1 of this ticket had `.crt` in the
#: entrypoint and not here, which made every `.crt` in every layer invisible to
#: this scan and made one branch of the exemption assertion unreachable.
CREDENTIAL_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".crt",
    ".cer",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".ppk",
)

#: The subset of the above that a *certificate* may legitimately use. A
#: trust-store entry must be one of these before its location is even consulted.
CERTIFICATE_SUFFIXES: tuple[str, ...] = (".pem", ".crt", ".cer")

#: Directories whose mere presence in the image would mean credentials came
#: along with them.
CREDENTIAL_DIRECTORY_NAMES: frozenset[str] = frozenset({".ssh", ".gnupg", ".aws", ".docker"})

#: The one exemption, and it is narrow on purpose.
#:
#: Debian's `ca-certificates` package ships root CA certificates under these
#: directories. They are the **public** trust store: published certificates that
#: contain no private key and are what makes TLS verification possible at all.
#: Removing them would not improve the image; it would make it unable to verify
#: anybody.
#:
#: **Location is necessary and nowhere near sufficient.** Round 1 of this ticket
#: exempted on location alone, and `etc/ssl/certs/` promptly became a hiding
#: place: an ADC file, a `.env`, an `id_rsa` and a `.pem` holding a private key
#: all reported clean. `is_public_trust_store_entry` is the authority on what is
#: exempt and checks the **name first**, then this location, then link target or
#: content. Read its docstring, not this comment, for the rule.
#:
#: The tests that fail if this exemption widens:
#: `test_the_only_exempted_files_are_real_public_certificates` (all conditions,
#: against the real image), `test_a_credential_hidden_in_the_trust_store_is_still_caught`
#: (each hiding attempt, including a key past the read cap),
#: `test_a_symlink_out_of_the_trust_store_is_not_exempted`, and
#: `test_a_genuine_public_certificate_is_still_exempted` so none of them can be
#: satisfied by exempting nothing.
PUBLIC_TRUST_STORE_PREFIXES: tuple[str, ...] = (
    "etc/ssl/certs/",
    "usr/share/ca-certificates/",
    "usr/local/share/ca-certificates/",
)

#: Two more published CA bundles that live outside those directories, exempted
#: by exact path rather than by pattern so the exemption cannot widen by
#: accident. `usr/lib/ssl/cert.pem` is Debian's symlink to the system bundle;
#: the second is the copy of `certifi` vendored inside the base image's own pip.
#: Both are public root certificates. The python version in the path is left in
#: deliberately: a base-image upgrade should make someone re-read this line.
PUBLIC_TRUST_STORE_FILES: frozenset[str] = frozenset(
    {
        "usr/lib/ssl/cert.pem",
        "usr/local/lib/python3.12/site-packages/pip/_vendor/certifi/cacert.pem",
    }
)

#: Suffixes that are private-key-shaped rather than certificate-shaped. These
#: are never certificate-shaped, so they never reach the exemption at all.
PRIVATE_KEY_SUFFIXES: tuple[str, ...] = (".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk")

#: Content markers. Assembled from fragments rather than written whole because
#: the CI credential scan greps tracked files for `BEGIN <...> PRIVATE KEY` and
#: tests are deliberately not exempt from it.
PEM_PRIVATE_KEY_MARKER = b"PRIVATE KEY-----"
PEM_CERTIFICATE_MARKER = b"BEGIN CERTIFICATE-----"

#: How much of a member is kept in memory to look for the **certificate**
#: marker. A PEM bundle declares itself in its first block, so a prefix is
#: enough to answer "does this look like a certificate file at all".
#:
#: It is emphatically **not** enough to answer "does this file also contain a
#: private key", and an earlier version of this file used it for both. The
#: reviewer appended a key past the cap to `etc/ssl/certs/ca-certificates.crt`
#: — a concatenated bundle, the single most appendable file in the image, and
#: one of two real exempted members larger than the cap — and the scan exempted
#: it. The private-key marker is therefore sought over the **whole** member, by
#: streaming, with memory bounded by the chunk size rather than the file size.
MAX_INSPECTED_BYTES = 64 * 1024

#: Streaming chunk size for the whole-member private-key search.
STREAM_CHUNK_BYTES = 256 * 1024

#: The launch policy the Dockerfile must declare, value for value. README.md
#: explains each one; this test is what stops the explanation drifting from the
#: image. A change here is a change to what an operator may do to an attested
#: workload, so it should be hard to make by accident.
EXPECTED_LAUNCH_POLICY: dict[str, str] = {
    "tee.launch_policy.allow_capabilities": "false",
    "tee.launch_policy.allow_cgroups": "false",
    "tee.launch_policy.allow_cmd_override": "false",
    "tee.launch_policy.allow_env_override": "MCPFORGE_RUN_ID,MCPFORGE_ATTESTATION_AUDIENCE",
    "tee.launch_policy.log_redirect": "never",
    "tee.launch_policy.monitoring_memory_allow": "never",
}


# ---------------------------------------------------------------------------
# Availability, and the deliberate refusal to skip silently
# ---------------------------------------------------------------------------


def _docker_unavailable_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker is not installed"
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if probe.returncode != 0:
        return f"the docker daemon is not reachable: {probe.stderr.strip()[:200]}"
    return None


def _require_docker_or_skip() -> None:
    reason = _docker_unavailable_reason()
    if reason is None:
        return
    message = f"the workload image tests need Docker — {reason}"
    if os.environ.get("MCPFORGE_IMAGE_TESTS_REQUIRED") == "1":
        pytest.fail(f"MCPFORGE_IMAGE_TESTS_REQUIRED=1 and {message}")
    pytest.skip(message)


@pytest.fixture(scope="session")
def built_image() -> Iterator[str]:
    """Build the image with the real `build.sh` and yield its local tag.

    This is also the acceptance criterion "the image builds from a clean
    checkout" — it runs the shipped script, not a bespoke docker command, so a
    broken script fails here rather than only in an operator's hands. `build.sh`
    with no arguments does not push.
    """

    _require_docker_or_skip()

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(BUILD_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    if result.returncode != 0:
        message = (
            f"build.sh failed:\nstdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
        if os.environ.get("MCPFORGE_IMAGE_TESTS_REQUIRED") == "1":
            pytest.fail(message)
        # A cold build needs to fetch the pinned base. Offline is a real and
        # ordinary reason to be unable to build, and it is named rather than
        # swallowed. CI sets the variable above, so this cannot hide a break.
        pytest.skip(f"could not build the workload image (offline?): {message[:1500]}")

    assert "digest: sha256:" in result.stdout, result.stdout
    assert "Not pushed" in result.stdout, "build.sh must not push without --push"
    yield LOCAL_TAG


@pytest.fixture(scope="session")
def image_manifest(built_image: str) -> dict[str, Any]:
    """The manifest of the OCI layout `build.sh` wrote — the pinned artefact.

    Read from the layout rather than from the daemon on purpose: the layout is
    the thing whose digest `build.sh` prints and the deployment pins, and it is
    built with rewritten timestamps, so it is not byte-identical to the copy
    `--load` puts in the daemon.
    """

    del built_image
    index = json.loads((OCI_LAYOUT / "index.json").read_text())
    manifests = index["manifests"]
    assert len(manifests) == 1, f"expected a single-platform manifest, got {len(manifests)}"
    return _read_blob_json(manifests[0]["digest"])


@pytest.fixture(scope="session")
def image_config(image_manifest: dict[str, Any]) -> dict[str, Any]:
    return _read_blob_json(image_manifest["config"]["digest"])


def _blob_path(digest: str) -> Path:
    algorithm, _, hex_digest = digest.partition(":")
    return OCI_LAYOUT / "blobs" / algorithm / hex_digest


def _read_blob_json(digest: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(_blob_path(digest).read_text())
    return payload


def _docker_run(
    image: str, environment: dict[str, str], *, extra_args: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "run", "--rm", "--network", "none"]
    for name, value in environment.items():
        argv += ["-e", f"{name}={value}"]
    argv += [*extra_args, image]
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, text=True, check=False, timeout=300
    )


# ---------------------------------------------------------------------------
# 1. Static — no Docker required
# ---------------------------------------------------------------------------


def test_every_base_image_is_pinned_by_digest() -> None:
    """A tag is a mutable pointer. An image built on one attests nothing.

    Both stages are checked, not just the final one: a builder stage that
    produced the site-packages tree from a moving base would put unknown bytes
    inside the trust boundary just as effectively.
    """

    from_lines = [
        line.strip()
        for line in DOCKERFILE.read_text().splitlines()
        if line.strip().upper().startswith("FROM ")
    ]
    assert from_lines, "no FROM instruction found — the test is not reading the Dockerfile"
    for line in from_lines:
        reference = line.split()[1]
        assert "@sha256:" in reference, f"base image is not digest-pinned: {line}"
        digest = reference.split("@", 1)[1]
        assert len(digest) == len("sha256:") + 64, f"malformed digest in {line}"
        assert digest.removeprefix("sha256:").islower(), f"digest is not canonical in {line}"


# ---------------------------------------------------------------------------
# Reading a file at the position a property actually lives
#
# Round 4 of this ticket found three tests that passed by matching a string
# *somewhere in a file* rather than where the property is enforced. The
# Dockerfile documents `--require-hashes` in a comment above the instruction
# that uses it, so `"--require-hashes" in dockerfile` stayed true after both
# flags were deleted from the `RUN pip install` line itself — the image would
# have installed unverified artefacts with the suite green. The comment
# describing a safeguard satisfied the test for the safeguard.
#
# These helpers exist so an assertion can name the instruction it means.
# ---------------------------------------------------------------------------


def _load_module(path: Path, name: str) -> ModuleType:
    """Import a module that is not on the package path, by file."""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: `infra/confidential-space/dockerfile_scan.py` — the **one** Dockerfile
#: parser and the **one** build-secret keyword list, shared with `build.sh`,
#: which runs the same file as a script before it builds.
#:
#: This used to be a second implementation here and a `sed`/`grep` pair there,
#: and the two drifted until a credential could walk between them. The four
#: measured escapes are recorded in that module's docstring; the two that
#: mattered most were a comment line ending in `\` (Docker drops comment lines
#: *before* joining continuations; neither guard did) and a continuation split
#: mid-token (Python joined with a space, Docker joins with nothing).
#:
#: It is also, deliberately, **not** the control any more. Text parsing catches
#: only what it can spell. The control is the artefact:
#: `test_the_image_config_declares_exactly_the_documented_environment` and
#: `test_the_final_stage_ran_exactly_the_documented_instructions` read the
#: built image's own config, which is what the digest covers.
dockerfile_scan = _load_module(INFRA_DIR / "dockerfile_scan.py", "mcpforge_dockerfile_scan")


#: The word `install`, as a whole word. `--no-install-recommends` does not match.
_INSTALL_TOKEN = re.compile(r"(?<![\w-])install(?![\w-])")


def dockerfile_instructions() -> list[str]:
    """The Dockerfile's instructions, parsed the way Docker parses them.

    Comment lines are dropped **first**, then continuations are joined with the
    empty string, then each instruction's internal whitespace is collapsed. All
    three steps live in `dockerfile_scan.instructions`; this is a thin call so
    that this file and `build.sh` cannot disagree about what an instruction is.
    """

    parsed: list[str] = dockerfile_scan.instructions(DOCKERFILE.read_text())
    return parsed


def package_install_instruction() -> str:
    """The single `RUN` instruction that installs anything, as one line.

    Not a list of pip spellings. The previous version enumerated `uv pip`,
    `python -m pip` and `pip<digits>`, and review measured four ordinary
    spellings it missed: `pip --no-cache-dir install` — the flag order this
    Dockerfile itself uses — `pip -q install`, `pip3.12 install` (that binary
    exists in the base image) and `python -mpip install`. A second, unpinned
    install therefore passed the "exactly one" assertion. `$PIP install`
    defeats any such regex outright, which is why the enumeration was abandoned
    rather than extended.

    The rule is now the weakest one that cannot be spelled around at this
    level: any `RUN` instruction containing the word `install` is an install.
    Inside an attested image every install is a supply-chain event, so a second
    one is a finding whatever it installs, `apt-get` included.

    **Its bound, and why it is not the control.** Two installs chained inside
    one `RUN` are still one instruction, and `$PIP install` is still invisible
    to the word match if the variable is what carries it. The control is
    `test_the_installed_distributions_are_exactly_the_pinned_closure`, which
    reads what is actually in the image's `site-packages` and compares it to
    `requirements.txt`, and so holds however the install was spelled.
    """

    installs = [
        line
        for line in dockerfile_instructions()
        if dockerfile_scan.verb(line) == "RUN" and _INSTALL_TOKEN.search(line.casefold())
    ]
    assert len(installs) == 1, f"expected exactly one install instruction, found {len(installs)}"
    return installs[0]


#: A here-document redirection: `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`.
#: The negative lookahead keeps a here-*string* (`<<<`) out of it.
_HEREDOC = re.compile(r"<<(-?)\s*(?!<)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _strip_shell_comment(line: str) -> str:
    """`line` up to the first `#` that actually begins a comment.

    What it models: single quotes (no escapes inside them), double quotes
    (backslash escapes honoured), backslash escapes outside quotes, and the
    rule that `#` opens a comment only at the start of a word — the start of
    the line, or after whitespace or one of `;|&(`.

    Escapes are tracked because without them a `\\"` inside a double-quoted
    string closes the string in the tracker's view but not in the shell's,
    leaving the quote state open so that a following `#` is never stripped.
    That was the real defect; the docstring here previously excluded it by
    asserting `build.sh` had "no escaped quotes outside strings", which named
    the wrong case.

    What it does not model: `$(...)`, `${...}`, arithmetic, and
    here-documents. `build.sh` does contain a here-document — `python3 - "$1"
    <<'PY'` — and the earlier version of this docstring said it did not.
    Here-document bodies never reach this function: `build_script_code` removes
    them, because that body is Python rather than shell and a string literal in
    it must not be able to satisfy an assertion about `build.sh`.
    """

    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            if char == "'":
                quote = None
        elif quote == '"':
            if char == "\\":
                index += 1
            elif char == '"':
                quote = None
        elif char == "\\":
            index += 1
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t;|&("):
            return line[:index]
        index += 1
    return line


def build_script_code() -> str:
    """`build.sh` with comments removed — whole-line **and** trailing.

    An earlier version stripped only whole-line comments and argued the
    remainder was harmless because the assertions "name a line shape". It was
    not harmless: naming the line shape does not help when the comment is *on*
    that line. Replacing the array definition with

        local -a canonical=("${common[@]}")  # --no-cache dropped for speed

    left `test_every_pinnable_build_runs_with_a_cold_cache` green with the
    property gone, which per cause 3 in `03_SECURITY_ACCESS.md` also
    re-vacuums the cause-1 and cause-2 reproducibility tests. A comment
    describing a safeguard must never satisfy the test for the safeguard —
    that sentence is the whole review record of this ticket.

    Here-document bodies are removed with their delimiter lines. `build.sh`
    embeds Python in one (`python3 - "$1" <<'PY'`), and embedded Python is not
    shell code: leaving it in means a string literal there could satisfy an
    assertion about `build.sh`, and it also feeds `_strip_shell_comment` input
    whose quoting rules are not the ones it models. Nothing is mis-stripped
    today — no `#` appears in that body — which is exactly the kind of accident
    that stops being true on the next edit.
    """

    lines = BUILD_SCRIPT.read_text().splitlines()
    code: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip().startswith("#"):
            continue
        stripped = _strip_shell_comment(line)
        code.append(stripped)

        heredoc = _HEREDOC.search(stripped)
        if heredoc is None:
            continue
        indented, _, delimiter = heredoc.groups()
        terminated = False
        while index < len(lines):
            body = lines[index]
            index += 1
            if (body.strip() if indented else body) == delimiter:
                terminated = True
                break
        assert terminated, f"unterminated here-document <<{delimiter} in build.sh"
    return "\n".join(code)


def test_the_dockerfile_declares_no_build_secret() -> None:
    """No ARG, ENV or secret mount that could carry a credential into a layer.

    **Defence in depth, not the control.** It runs the same scanner `build.sh`
    runs, over the same Docker-faithful parse, with the same keyword list — one
    rule, one list, so the two cannot drift the way they did. But a text scan
    catches only what it can spell. The controls are
    `test_the_image_config_declares_exactly_the_documented_environment` and
    `test_the_final_stage_ran_exactly_the_documented_instructions`, which read
    the built image's own config and history rather than the recipe.
    """

    instructions = dockerfile_instructions()
    assert instructions, "no instructions found — the test is not reading the Dockerfile"
    scanned = [line for line in instructions if dockerfile_scan.verb(line) in ("ARG", "ENV", "RUN")]
    assert scanned, "no ARG/ENV/RUN instruction found — the parse is returning nothing useful"

    findings: list[str] = dockerfile_scan.build_secret_findings(DOCKERFILE.read_text())
    assert not findings, f"the Dockerfile appears to carry a build secret: {findings}"


#: Every payload that got a credential past one or both of the two previous
#: guards, measured on a real build. Each is a complete Dockerfile fragment.
_MEASURED_BUILD_SECRET_ESCAPES: tuple[tuple[str, str], ...] = (
    (
        "a comment line ending in a backslash",
        # Docker drops the comment line, then executes the ENV. Both guards
        # joined first, so the ENV was swallowed into the comment and vanished:
        # the helper returned only ['FROM python:3.12-slim-bookworm'] and the
        # shell guard printed nothing, while the variable was in the image.
        "FROM python:3.12-slim-bookworm\n"
        "# a note that happens to end here \\\n"
        "ENV GEMINI_API_KEY=x\n",
    ),
    (
        "a continuation split mid-token",
        # Python joined with a space, so `..._K` + `EY=` never became one word.
        "FROM scratch\nENV GEMINI_API_K\\\nEY=x\n",
    ),
    (
        "a lowercase instruction",
        # Docker's verbs are case-insensitive; the shell guard's grep was not.
        "FROM scratch\nenv gemini_api_key=x\n",
    ),
    (
        "a lowercase instruction split mid-token",
        # Caught by neither guard: the case defeated one and the space the other.
        "FROM scratch\nenv gemini_api_k\\\ney=x\n",
    ),
    (
        "a bare KEY suffix",
        # The Python list held API_KEY and PRIVATE_KEY but no bare KEY.
        "FROM scratch\nENV GEMINI_KEY=x\n",
    ),
    (
        "a hash inside an earlier value",
        # The shell guard matched `[^#]*`, so a `#` in an earlier value ended
        # the match before the credential.
        'FROM scratch\nENV COLOUR="#ff0000" GEMINI_API_KEY=x\n',
    ),
    (
        "a mounted build secret",
        "FROM scratch\nRUN --mount=type=secret,id=gemini cat /run/secrets/gemini\n",
    ),
)


@pytest.mark.parametrize(("description", "text"), _MEASURED_BUILD_SECRET_ESCAPES)
def test_a_measured_build_secret_escape_is_reported(description: str, text: str) -> None:
    """Every payload that previously walked past a guard is now reported.

    These are not hypotheticals. Each one was measured against the guards that
    existed before, and the first two put a real variable into the attested
    image's config with the whole suite green — a credential inside the trust
    boundary, which is CLAUDE.md non-negotiable 2.

    The scanner under test is the one `build.sh` runs, so a pass here is a pass
    for both callers.
    """

    findings: list[str] = dockerfile_scan.build_secret_findings(text)
    assert findings, f"{description} was not reported"


def test_the_dockerfile_parser_follows_dockers_own_order() -> None:
    """Comment lines are dropped first; continuations then join with nothing.

    Both halves are load-bearing and both were wrong before. Joining first lets
    a comment ending in `\\` delete the instruction after it — Docker keeps that
    instruction. Joining with a space instead of nothing lets a token be split
    across the break and never reassembled.
    """

    instructions: list[str] = dockerfile_scan.instructions(
        "FROM scratch\n"
        "# a comment that ends in a continuation \\\n"
        "ENV KEPT=1\n"
        "ENV SPL\\\n"
        "IT=2\n"
        "RUN one \\\n"
        "    # an interior comment line\n"
        "    && two\n"
    )
    assert instructions == [
        "FROM scratch",
        "ENV KEPT=1",
        "ENV SPLIT=2",
        "RUN one && two",
    ], instructions


def test_the_dockerfile_parser_refuses_an_escape_directive() -> None:
    """It models `\\` continuations only, so it refuses a file that changes them.

    Fail-closed. A security scan that silently mis-parses its input is worse
    than one that will not run.
    """

    with pytest.raises(dockerfile_scan.DockerfileParseError):
        dockerfile_scan.instructions("# escape=`\nFROM scratch\nENV A=1\n")


def test_the_dependency_closure_is_hash_pinned() -> None:
    """`--require-hashes`, `--no-deps`, and a hash on every pin.

    Both anchor documents assert that dependencies are hash-pinned, and until
    round 3 nothing checked it — an asserted security property with no test
    behind it, which is the exact failure mode the review record on this ticket
    keeps returning to. A substituted or re-uploaded artefact must fail the
    build rather than enter the trust boundary silently.
    """

    # Asserted against the install instruction, never the whole file: the
    # comment above it names both flags, and matched the file after they were
    # deleted from the command.
    install = package_install_instruction()
    assert "--require-hashes" in install, (
        f"pip may install unverified artefacts — flag absent from: {install}"
    )
    assert "--no-deps" in install, (
        f"pip would resolve dependencies outside the closure — flag absent from: {install}"
    )

    # `uv pip compile --generate-hashes` writes each requirement as one logical
    # line continued with backslashes, so the continuations are joined before
    # anything is asserted about a "requirement".
    joined = (INFRA_DIR / "requirements.txt").read_text().replace("\\\n", " ")
    pins = [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(pins) >= 5, f"only {len(pins)} requirement lines — reading the wrong file"
    for pin in pins:
        assert "==" in pin, f"requirement is not pinned to an exact version: {pin}"
        assert "--hash=sha256:" in pin, f"pinned version carries no hash: {pin}"


def test_every_launch_policy_label_is_explained_in_the_readme() -> None:
    """The documents claim each label is explained line by line. This checks it.

    `test_the_launch_policy_labels_are_exactly_as_documented` compares the image
    against a constant in this file, which says nothing about whether the README
    still describes them. Both anchor documents make the "explained line by
    line" claim, so it gets a test rather than a promise.
    """

    readme = README.read_text()
    # The *value*, not only the name. Round 4: the README was changed to say
    # `log_redirect`="always" and "stdout and stderr are ALWAYS redirected"
    # while the image set "never", and a name-only check passed. An operator
    # reads this document to learn what the constraint is, so a description
    # that contradicts the image is the defect, not a typo.
    for name, value in EXPECTED_LAUNCH_POLICY.items():
        fence = f'LABEL "{name}"="{value}"'
        assert fence in readme, (
            f"README.md does not state {name}={value!r} as the image sets it "
            f"(expected the line {fence})"
        )
    # The one deliberately undeclared label must be explained as an absence,
    # otherwise a reader cannot tell it was a decision.
    assert "tee.launch_policy.allow_mount_destinations" in readme


def test_nothing_is_copied_wholesale_into_the_image() -> None:
    """Every COPY names a path. `COPY . .` would drag the repository in."""

    copies = [
        line.strip()
        for line in DOCKERFILE.read_text().splitlines()
        if line.strip().upper().startswith("COPY ")
    ]
    assert copies, "no COPY instruction found — the test is not reading the Dockerfile"
    for line in copies:
        sources = [token for token in line.split()[1:-1] if not token.startswith("--")]
        assert sources, f"COPY with no source: {line}"
        for source in sources:
            assert source not in {".", "./", "*"}, f"wholesale COPY: {line}"


def test_the_build_context_is_an_allowlist() -> None:
    """`Dockerfile.dockerignore` excludes everything, then re-includes by name.

    The context is the repository root, which holds `.env`. Excluding known-bad
    names is a list that goes stale; excluding everything and naming what is
    needed does not.
    """

    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "the dockerignore is empty — the test is not reading it"
    assert lines[0] == "*", "the first rule must exclude everything"
    assert any(line == "**/.env" for line in lines), ".env must be excluded unconditionally"


def test_the_entrypoint_is_exec_form_in_the_dockerfile() -> None:
    """JSON array form. `ENTRYPOINT python3 x.py` would run under `sh -c`."""

    entrypoints = [
        line.strip()
        for line in DOCKERFILE.read_text().splitlines()
        if line.strip().upper().startswith("ENTRYPOINT")
    ]
    assert len(entrypoints) == 1, f"expected exactly one ENTRYPOINT, got {entrypoints}"
    argv = json.loads(entrypoints[0][len("ENTRYPOINT") :].strip())
    assert isinstance(argv, list), "ENTRYPOINT must be exec form"
    assert Path(argv[0]).name not in {"sh", "bash", "dash", "ash", "zsh"}
    assert "-c" not in argv


def test_build_script_pushes_to_the_declared_repository_and_not_by_default() -> None:
    """The push target is the one the ticket names, and it is opt-in.

    `build.sh` must contain a correct push step; running it is a spend decision
    for the project owner, so the default path must not take it.
    """

    code = build_script_code()
    assert 'REGISTRY="us-central1-docker.pkg.dev"' in code
    assert 'PROJECT="mcpforge-aa5c2"' in code
    assert 'REPOSITORY="mcpforge-executor"' in code
    assert "push=true" in code, "build.sh has no push step"
    assert "--push" in code, "the push must be behind an explicit flag"
    # The one line that pushes is guarded by the flag, not run unconditionally.
    push_lines = [line for line in code.splitlines() if "push=true" in line]
    assert len(push_lines) == 1, f"expected one push, found {len(push_lines)}"


def test_build_script_verifies_the_pushed_digest_against_the_local_build() -> None:
    """A pinned digest is only meaningful if the registry holds the same bytes."""

    # Comments in build.sh discuss `rewrite-timestamp` and `SOURCE_DATE_EPOCH`
    # by name, so the whole-file form of these assertions survived deleting the
    # mechanisms. Same defect class as the pip flags; same fix.
    code = build_script_code()
    assert "rewrite-timestamp=true" in code, "timestamps must be rewritten for reproducibility"
    assert 'export SOURCE_DATE_EPOCH="${epoch}"' in code, (
        "build.sh must set SOURCE_DATE_EPOCH, not merely mention it"
    )
    assert 'if [[ "${pushed}" != "${digest}" ]]; then' in code


def test_every_pinnable_build_runs_with_a_cold_cache() -> None:
    """`--no-cache` on both builds whose digest can be pinned.

    This is not hygiene. Removing `--no-cache` from `build.sh` left the whole
    file green, and removing it *together with* the mtime normalisation made
    `test_the_digest_does_not_depend_on_source_file_mtimes` and
    `test_every_workload_file_has_a_normalised_mtime` both pass over the
    reinstated round-1 defect: with the cache live, BuildKit exports a pre-fix
    layer, so the tests read an artefact the current Dockerfile does not
    produce. A stale cache already put a wrong digest in this README once.

    The `--load` build is deliberately excluded: it is warmed by the canonical
    build and its digest is never pinned.
    """

    code = build_script_code().replace("\\\n", " ")
    lines = [" ".join(line.split()) for line in code.splitlines() if line.strip()]

    canonical = [line for line in lines if line.startswith("local -a canonical=")]
    assert len(canonical) == 1, f"expected one canonical array, found {len(canonical)}"
    assert "--no-cache" in canonical[0], (
        f"the pinnable build may reuse a stale layer: {canonical[0]}"
    )

    invocations = [line for line in lines if line.startswith("docker ") and "--output" in line]
    assert len(invocations) == 2, f"expected two --output builds, found {len(invocations)}"
    for line in invocations:
        assert '"${canonical[@]}"' in line, (
            f"a build that produces a pinnable digest does not use the cold-cache "
            f"argument array: {line}"
        )


# ---------------------------------------------------------------------------
# 2. Artefact — the real OCI layout and its real layers
# ---------------------------------------------------------------------------


def test_the_image_builds_and_produces_one_amd64_manifest(image_manifest: dict[str, Any]) -> None:
    """Acceptance: the image builds from a clean checkout.

    Single-platform on purpose. A multi-platform build produces an index digest,
    and an attestation claim carries one image digest — pinning an index would
    pin something the token never names.
    """

    assert image_manifest["layers"], "the manifest declares no layers"
    index = json.loads((OCI_LAYOUT / "index.json").read_text())
    platform = index["manifests"][0]["platform"]
    assert platform["architecture"] == "amd64"
    assert platform["os"] == "linux"


def test_the_launch_policy_labels_are_exactly_as_documented(image_config: dict[str, Any]) -> None:
    """Every `tee.launch_policy.*` label, value for value.

    These are what an operator is permitted to change about an attested
    workload. `allow_cmd_override=false` in particular is what stops the pinned
    digest from meaning "this image, running something else".
    """

    labels: dict[str, str] = image_config["config"]["Labels"]
    for name, expected in EXPECTED_LAUNCH_POLICY.items():
        assert labels.get(name) == expected, (
            f"{name} is {labels.get(name)!r}, expected {expected!r}"
        )

    declared = {name for name in labels if name.startswith("tee.launch_policy.")}
    assert declared == set(EXPECTED_LAUNCH_POLICY), (
        "an undocumented launch-policy label is present or a documented one is missing: "
        f"{declared ^ set(EXPECTED_LAUNCH_POLICY)}"
    )
    assert "tee.launch_policy.allow_mount_destinations" not in labels, (
        "declaring it, even empty, is a value rather than an absence"
    )


def test_the_image_runs_as_a_fixed_non_root_user(image_config: dict[str, Any]) -> None:
    """03_SECURITY_ACCESS.md §3. A numeric uid, so it cannot resolve to root."""

    user = image_config["config"]["User"]
    assert user == "10001:10001", f"USER is {user!r}"


def test_the_image_entrypoint_is_exec_form_with_no_shell(image_config: dict[str, Any]) -> None:
    argv = image_config["config"]["Entrypoint"]
    assert argv == ["python3", "/opt/mcpforge/entrypoint.py"], argv
    assert not image_config["config"].get("Cmd"), "there is nothing to override"


def test_the_image_carries_the_executor_and_not_the_service(image_config: dict[str, Any]) -> None:
    """Minimal: the executor workload only, not FastAPI and not the web tier."""

    del image_config
    members = _all_layer_member_names()
    modules = {name for name in members if "/mcpforge/" in f"/{name}"}
    assert any(name.endswith("mcpforge/execution/development.py") for name in modules), (
        "the secure executor is not in the image"
    )
    # The relying party verifies the workload's token; it must not ship inside
    # the thing it judges.
    for forbidden in (
        "mcpforge/api/",
        "mcpforge/agents/",
        "mcpforge/gemini/",
        "mcpforge/github/",
        "mcpforge/relying_party/",
    ):
        offenders = sorted(name for name in modules if forbidden in name)
        assert not offenders, f"{forbidden} should not be inside the trust boundary: {offenders}"
    for forbidden_package in ("fastapi/", "uvicorn/", "google/genai/", "tree_sitter/"):
        offenders = sorted(name for name in members if f"/{forbidden_package}" in f"/{name}")
        assert not offenders, f"{forbidden_package} is not part of the workload: {offenders[:5]}"


# --- the artefact-level build-secret control -------------------------------
#
# Everything above about the Dockerfile is text. Text is what six review rounds
# on this ticket kept getting past: a comment ending in `\`, a token split
# across a continuation, a lowercase verb, a keyword the list did not hold. Two
# of those put a real variable into the attested image's config with the whole
# suite green.
#
# These two tests are the control, and they are spelling-proof because they do
# not read the recipe. `Config.Env` is what the image actually carries and is
# part of what the digest covers; the config history is the record of the
# instructions that produced it. However an ENV is spelled, it lands here.
# ---------------------------------------------------------------------------


#: Every environment variable in the built image, name and value. Asserted
#: **exactly**: an extra variable fails, a missing one fails, a changed value
#: fails. That is the property, and it is why no keyword list is involved.
#:
#: The first five come from the pinned base image. `GPG_KEY` is the public
#: fingerprint of the Python release signing key and `PYTHON_SHA256` is a source
#: checksum — both are published values, neither is a credential, and both are
#: listed here rather than excused by a pattern so that nothing is excused by a
#: pattern. Moving the base pin changes these and is meant to fail this test:
#: it also changes the image digest, which is a decision, not a rebuild.
EXPECTED_IMAGE_ENV: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "GPG_KEY": "7169605F62C751356D054A26A821E680E5FA6305",
    "PYTHON_VERSION": "3.12.14",
    "PYTHON_SHA256": "5c8462af5790baf43a321a1559dbe0db06d1be4300fb85fb53c40060668e548a",
    "PYTHONPATH": "/opt/mcpforge/site-packages",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "HOME": "/home/mcpforge",
}

#: `rewrite-timestamp=true` with `SOURCE_DATE_EPOCH=0` stamps every layer this
#: build creates with the epoch, and leaves the pinned base image's own history
#: at its real dates. That is what separates our instructions from the base's.
_EPOCH_TIMESTAMP = "1970-01-01T00:00:00Z"


def test_the_image_config_declares_exactly_the_documented_environment(
    image_config: dict[str, Any],
) -> None:
    """`Config.Env` is exactly `EXPECTED_IMAGE_ENV`, name and value.

    This is the artefact-level answer to the whole build-secret defect class.
    A `GEMINI_API_KEY` reached the image config three different ways during
    review — through a comment ending in `\\`, through a token split across a
    continuation, and through a lowercase `env` — and every one of them was
    invisible to a check that parsed the Dockerfile. None of them is invisible
    here, because an environment variable that exists is in this list or the
    test fails, whatever the recipe looked like.
    """

    declared: list[str] = image_config["config"]["Env"]
    assert declared, "the image declares no environment at all — reading the wrong config"

    actual: dict[str, str] = {}
    for entry in declared:
        name, separator, value = entry.partition("=")
        assert separator, f"malformed environment entry: {entry!r}"
        assert name not in actual, f"{name} is declared twice: {declared}"
        actual[name] = value

    changed = {
        name
        for name in set(actual) & set(EXPECTED_IMAGE_ENV)
        if actual[name] != EXPECTED_IMAGE_ENV[name]
    }
    assert actual == EXPECTED_IMAGE_ENV, (
        "the image's environment is not the documented one; every difference is a "
        "value inside the trust boundary: "
        f"unexpected={sorted(set(actual) - set(EXPECTED_IMAGE_ENV))} "
        f"missing={sorted(set(EXPECTED_IMAGE_ENV) - set(actual))} "
        f"changed={sorted(changed)}"
    )


#: Every instruction of the final stage, exactly as the builder recorded it in
#: the image config. Asserted as a list, in order, so that an added, removed or
#: altered instruction fails — a keyword list is not involved, and neither is
#: any judgement about what a credential looks like.
#:
#: A keyword scan was tried here first and was wrong for this input: the
#: description label contains "token" and the user-creation `RUN` writes to
#: `/etc/passwd`, so it produced two false positives immediately. False
#: positives are how a keyword list gets shortened, and a shortened list is the
#: defect this ticket has now had three rounds of.
EXPECTED_STAGE_HISTORY: tuple[str, ...] = (
    "LABEL tee.launch_policy.allow_capabilities=false",
    "LABEL tee.launch_policy.allow_cgroups=false",
    "LABEL tee.launch_policy.allow_cmd_override=false",
    "LABEL tee.launch_policy.allow_env_override=MCPFORGE_RUN_ID,MCPFORGE_ATTESTATION_AUDIENCE",
    "LABEL tee.launch_policy.log_redirect=never",
    "LABEL tee.launch_policy.monitoring_memory_allow=never",
    "LABEL org.opencontainers.image.title=mcpforge-confidential-space-workload",
    "LABEL org.opencontainers.image.source=https://github.com/tony19053000/MCPForge",
    "LABEL org.opencontainers.image.description=MCPForge secure executor workload. "
    "Preflight, then obtains a Confidential Space attestation token and delivers it to "
    "the relying party; runs no repository job.",
    "RUN /bin/sh -c printf 'mcpforge:x:10001:\\n' >> /etc/group     && printf "
    "'mcpforge:x:10001:10001::/home/mcpforge:/usr/sbin/nologin\\n' >> /etc/passwd     "
    "&& mkdir -p /home/mcpforge /opt/mcpforge /workspace     && chown 10001:10001 "
    "/home/mcpforge /workspace # buildkit",
    "COPY --chown=10001:10001 /build/site-packages /opt/mcpforge/site-packages # buildkit",
    "COPY --chown=10001:10001 /build/app/entrypoint.py /opt/mcpforge/entrypoint.py # buildkit",
    "ENV PYTHONPATH=/opt/mcpforge/site-packages PYTHONDONTWRITEBYTECODE=1 "
    "PYTHONUNBUFFERED=1 HOME=/home/mcpforge",
    "USER 10001:10001",
    "WORKDIR /opt/mcpforge",
    'ENTRYPOINT ["python3" "/opt/mcpforge/entrypoint.py"]',
)


def test_the_final_stage_ran_exactly_the_documented_instructions(
    image_config: dict[str, Any],
) -> None:
    """The final stage's recorded history is exactly `EXPECTED_STAGE_HISTORY`.

    The second half of the artefact control. `Config.Env` covers what survived
    into the environment; this covers what the final stage was *told to do* —
    every `RUN`, `COPY`, `LABEL`, `ENV` and `ARG` as the builder recorded it,
    not as the Dockerfile spells it. A continuation, an interior comment or a
    lowercase verb is already resolved by the time it is written here, so none
    of the parsing tricks that beat the text guards mean anything to this test.

    **Two bounds, stated rather than implied.** BuildKit records only the final
    stage's instructions plus the base image's, so the `deps` stage's `pip
    install` does not appear — that stage is covered by
    `test_the_installed_distributions_are_exactly_the_pinned_closure` and by
    `test_no_layer_contains_credential_material`. And a `--mount=type=secret`
    is not written into `created_by` at all, so it is covered by the layer scan
    and by `test_the_dockerfile_declares_no_build_secret`.
    """

    history: list[dict[str, Any]] = image_config["history"]
    base = [entry for entry in history if entry.get("created") != _EPOCH_TIMESTAMP]
    ours = [entry for entry in history if entry.get("created") == _EPOCH_TIMESTAMP]

    # Self-guard: a split that put everything on one side would make this test
    # either vacuous or permanently red for the wrong reason.
    assert base, "no base-image history — the timestamp split is not doing what it claims"

    recorded = tuple(str(entry.get("created_by", "")) for entry in ours)
    assert recorded == EXPECTED_STAGE_HISTORY, (
        "the final stage did not run the documented instructions; every difference is "
        "an instruction inside the trust boundary: "
        f"unexpected={[line for line in recorded if line not in EXPECTED_STAGE_HISTORY]} "
        f"missing={[line for line in EXPECTED_STAGE_HISTORY if line not in recorded]}"
    )


# --- the installed closure, asserted positively ----------------------------


#: `<name>-<version>.dist-info` directly under the image's site-packages. A
#: PEP 440 version contains no `-`, so the split is unambiguous.
_DIST_INFO = re.compile(r"^opt/mcpforge/site-packages/([^/]+)-([^-/]+)\.dist-info(?:/|$)")

#: A requirement line's `name==version` head.
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")


def _canonical_distribution(name: str) -> str:
    """PEP 503 normalisation, so `typing-extensions` and `typing_extensions` agree."""

    return re.sub(r"[-_.]+", "-", name).lower()


def pinned_distributions() -> dict[str, str]:
    """`requirements.txt` as `{canonical name: version}`."""

    pins: dict[str, str] = {}
    for line in (INFRA_DIR / "requirements.txt").read_text().splitlines():
        match = _REQUIREMENT.match(line.strip())
        if match is not None:
            pins[_canonical_distribution(match.group(1))] = match.group(2)
    return pins


def installed_distributions(member_names: list[str]) -> dict[str, str]:
    """The distributions actually installed in the image, from their `.dist-info`."""

    installed: dict[str, str] = {}
    for name in member_names:
        match = _DIST_INFO.match(name)
        if match is not None:
            installed[_canonical_distribution(match.group(1))] = match.group(2)
    return installed


def test_the_installed_distributions_are_exactly_the_pinned_closure(
    image_manifest: dict[str, Any],
) -> None:
    """What is installed equals `requirements.txt`, both directions.

    This replaces counting install spellings, which review showed cannot be
    done: `pip --no-cache-dir install`, `pip -q install`, `pip3.12 install` and
    `python -mpip install` all escaped the regex that was there, and `$PIP
    install` escapes any regex. So the question stopped being "how many
    installs does the recipe contain" and became "what is in the image".

    Every install writes a `.dist-info`, so a second install — chained onto the
    first `RUN`, spelled through a variable, or added in a stage this file
    never reads — appears here as a distribution that `requirements.txt` does
    not pin. The reverse direction matters too: a pin that is not installed
    means the hash-pinned closure is not what the image actually runs.
    """

    del image_manifest  # the fixture builds the image; the members are read below
    installed = installed_distributions(_all_layer_member_names())
    pinned = pinned_distributions()

    assert len(pinned) >= 5, f"only {len(pinned)} pins parsed — reading the wrong file"
    assert len(installed) >= 5, f"only {len(installed)} distributions found — wrong artefact"

    mismatched = {name for name in set(installed) & set(pinned) if installed[name] != pinned[name]}
    assert installed == pinned, (
        "the image's installed closure is not the hash-pinned one: "
        f"unpinned={sorted(set(installed) - set(pinned))} "
        f"missing={sorted(set(pinned) - set(installed))} "
        f"version mismatch={sorted(mismatched)}"
    )


def test_an_unpinned_distribution_would_be_reported() -> None:
    """The comparison above is proved able to fail, without a second build.

    `test_the_installed_distributions_are_exactly_the_pinned_closure` compares
    two sets, and a reader cannot tell from a green run whether either side was
    read at all. This drives the same two functions over member names that
    contain an extra distribution and requires the difference to show.
    """

    smuggled = installed_distributions(
        [
            "opt/mcpforge/site-packages/anyio-4.14.2.dist-info/METADATA",
            "opt/mcpforge/site-packages/requests-2.32.3.dist-info/RECORD",
        ]
    )
    assert smuggled == {"anyio": "4.14.2", "requests": "2.32.3"}

    pinned = pinned_distributions()
    assert "anyio" in pinned, "requirements.txt is not being read"
    assert set(smuggled) - set(pinned) == {"requests"}, (
        "an install that requirements.txt does not pin would not be reported"
    )


# --- reproducibility, which is what makes a pinned digest mean anything ----


#: The files the Dockerfile copies out of the build context. A reproducibility
#: test builds from a synthetic context holding exactly these.
CONTEXT_PATHS: tuple[str, ...] = (
    "infra/confidential-space/requirements.txt",
    "infra/confidential-space/entrypoint.py",
    "services/api/src/mcpforge/__init__.py",
    "services/api/src/mcpforge/logging.py",
    "services/api/src/mcpforge/execution",
)


def _build_digest(context: Path, output: Path) -> str:
    return _build_digest_with(BUILD_SCRIPT, context, output)


def _build_digest_with(
    script: Path, context: Path, output: Path, *, epoch: str = "1234567890"
) -> str:
    """Run the real `build.sh` against a given context and return its digest.

    The real script, so the buildx flags under test are the shipped ones rather
    than a second copy that could drift — including `--no-cache`, without which
    this comparison is worthless. BuildKit's local-file cache key is
    content-based and ignores mtimes, so two builds sharing a cache reuse one
    another's layers and agree on a digest whether or not the image is actually
    reproducible. This test passed with the normalisation removed until
    `build.sh` stopped caching the canonical build.
    """

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(script)],
        cwd=script.parent,
        env={
            **os.environ,
            "MCPFORGE_BUILD_CONTEXT": str(context),
            "MCPFORGE_BUILD_OUTPUT": str(output),
            # Deliberately hostile: if build.sh ever reads an inherited
            # SOURCE_DATE_EPOCH instead of setting its own constant, a caller's
            # clock is back in the digest and this value would expose it.
            "SOURCE_DATE_EPOCH": epoch,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    digests = [
        line.split(":", 1)[1].strip()
        for line in result.stdout.splitlines()
        if line.startswith("digest:")
    ]
    assert len(digests) == 1, result.stdout
    return digests[0]


def _synthetic_context(root: Path, mtime: float) -> Path:
    """A build context holding exactly the copied paths, at a chosen mtime."""

    root.mkdir(parents=True, exist_ok=True)
    for relative in CONTEXT_PATHS:
        source = REPO_ROOT / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, destination)
    for path in sorted(root.rglob("*"), reverse=True):
        os.utime(path, (mtime, mtime))
    return root


def test_the_digest_does_not_depend_on_source_file_mtimes(built_image: str, tmp_path: Path) -> None:
    """Two contexts, identical content, mtimes five years apart, one digest.

    This is the round-1 defect and it is subtle. `COPY` preserves the source
    file's mtime, and `rewrite-timestamp=true` only clamps timestamps *newer*
    than `SOURCE_DATE_EPOCH` — it does not raise older ones. So a fresh `git
    clone`, whose files are always newer than HEAD's commit time, got clamped,
    while a working tree holding files from before the last commit did not. The
    reviewer measured two clones agreeing with each other and disagreeing with
    the working tree on exactly one layer, with `diff -r` of the extracted
    layers empty.

    A digest that depends on a developer's checkout history is not a digest
    anyone can pin: every ordinary rebuild would look like tampering. The
    Dockerfile normalises every copied mtime to the epoch, and this fails if
    that line is removed.
    """

    del built_image
    old = _synthetic_context(tmp_path / "old", mtime=1_577_836_800.0)  # 2020-01-01
    new = _synthetic_context(tmp_path / "new", mtime=1_893_456_000.0)  # 2030-01-01

    old_digest = _build_digest(old, tmp_path / "oci-old")
    new_digest = _build_digest(new, tmp_path / "oci-new")

    assert old_digest == new_digest, (
        "the image digest changed with nothing but source file mtimes: "
        f"{old_digest} vs {new_digest}"
    )


def _git(repository: Path, *arguments: str, when: str | None = None) -> None:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "F8-02a test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "F8-02a test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    if when is not None:
        environment["GIT_AUTHOR_DATE"] = when
        environment["GIT_COMMITTER_DATE"] = when
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *arguments],  # noqa: S607
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, f"git {arguments}: {result.stderr}"


def test_the_digest_does_not_depend_on_the_commit_timestamp(
    built_image: str, tmp_path: Path
) -> None:
    """An empty commit must not change the digest. It used to.

    `SOURCE_DATE_EPOCH` was HEAD's commit time, and `rewrite-timestamp=true`
    stamps build-created layers and the image config with it. So committing
    *anything at all* — including a commit that changes no file — produced a
    different image digest. The reviewer measured two digests from one clean
    clone separated by a single empty commit. That is fatal for a pinned value:
    the commit that lands this ticket would have invalidated the digest the
    ticket exists to produce.

    `test_the_digest_does_not_depend_on_source_file_mtimes` cannot see this,
    because both of its builds share one `REPO_ROOT` and therefore one epoch.
    This one builds from a real repository at two real HEADs, using each copy's
    own `build.sh`, which is the only arrangement in which the epoch can differ.
    """

    del built_image
    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    repository = tmp_path / "repo"
    _synthetic_context(repository, mtime=1_577_836_800.0)
    shutil.copytree(
        INFRA_DIR,
        repository / "infra" / "confidential-space",
        ignore=shutil.ignore_patterns(".build"),
        dirs_exist_ok=True,
    )
    script = repository / "infra" / "confidential-space" / "build.sh"
    script.chmod(0o755)

    _git(repository, "init", "-q")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "-m", "first", when="2020-01-01T00:00:00+0000")
    first = _build_digest_with(script, repository, tmp_path / "oci-head-1")

    _git(
        repository,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "empty, changes no file",
        when="2030-06-01T12:34:56+0000",
    )
    second = _build_digest_with(script, repository, tmp_path / "oci-head-2")

    assert first == second, (
        "an empty commit changed the image digest, so the value pinned into "
        f"AttestationPolicy.image_digest moves whenever anyone commits: {first} vs {second}"
    )


def test_every_workload_file_has_a_normalised_mtime(image_manifest: dict[str, Any]) -> None:
    """Every file MCPForge adds carries mtime 0, so the digest is content-only.

    The direct, fast form of the property that
    `test_the_digest_does_not_depend_on_source_file_mtimes` demonstrates
    end to end. If the normalising `find ... touch` leaves the Dockerfile, the
    members below carry their source mtimes and this fails.

    It reads the layers that were actually exported — but that is not the same
    as being unmaskable by a build cache, which this docstring used to claim.
    Removing the normalisation *and* `--no-cache` together leaves this test and
    the end-to-end one both green, because BuildKit then exports a pre-fix
    layer and both read an artefact the current Dockerfile does not produce.
    The cold cache is what makes this assertion mean anything, and it has its
    own test in `every_pinnable_build_runs_with_a_cold_cache`.

    Only `opt/mcpforge/**` is asserted. The base image's own timestamps are the
    base image's business, and they are fixed by its pinned digest.
    """

    ours: list[tuple[str, float]] = []
    for layer in image_manifest["layers"]:
        with tarfile.open(_blob_path(layer["digest"]), mode="r:*") as archive:
            for info in archive:
                name = info.name.removeprefix("./")
                if name.startswith("opt/mcpforge/") and name != "opt/mcpforge/":
                    ours.append((name, info.mtime))

    assert len(ours) > 100, f"only {len(ours)} workload members found — reading the wrong image"
    offenders = [(name, mtime) for name, mtime in ours if mtime != 0]
    assert not offenders, (
        "workload files carry a checkout-dependent mtime, so the digest is not "
        f"content-only: {offenders[:5]}"
    )


def test_an_inherited_source_date_epoch_is_ignored(built_image: str, tmp_path: Path) -> None:
    """`build.sh` sets `SOURCE_DATE_EPOCH`; it must never read one.

    The commit-timestamp test cannot see this on its own: if the script honoured
    an inherited value, both of that test's builds would inherit the *same*
    hostile value and still agree. So the invariant is pinned directly — a build
    with a hostile epoch in the environment must produce the digest the README
    records.
    """

    del built_image
    hostile = _build_digest_with(
        BUILD_SCRIPT, REPO_ROOT, tmp_path / "oci-hostile", epoch="1234567890"
    )
    index = json.loads((OCI_LAYOUT / "index.json").read_text())
    assert hostile == index["manifests"][0]["digest"], (
        "an inherited SOURCE_DATE_EPOCH changed the digest, so a caller's clock "
        "is part of the value pinned into AttestationPolicy.image_digest"
    )


def test_the_readme_records_the_digest_that_is_actually_built(built_image: str) -> None:
    """The recorded digest is the built one, or this fails.

    A digest in prose is the value someone will paste into
    `AttestationPolicy.image_digest`. A stale one is worse than none, because it
    looks authoritative. This is the test that fails if the README drifts, which
    is the discipline F8-01's review rounds arrived at: every asserted property
    names the test that would fail if it were violated.
    """

    del built_image
    index = json.loads((OCI_LAYOUT / "index.json").read_text())
    built = index["manifests"][0]["digest"]

    recorded = [
        line.split("digest:", 1)[1].strip()
        for line in README.read_text().splitlines()
        if line.strip().startswith("digest:")
    ]
    assert len(recorded) == 1, f"expected one recorded digest in the README, found {recorded}"
    assert recorded[0] == built, (
        f"README.md records {recorded[0]} but the build produces {built}; "
        "re-run ./build.sh and update the README"
    )


#: Every file whose prose may name a backend test.
#:
#: The two anchor documents are in this tuple because leaving them out is
#: precisely how a stale sentence survived round 3: `03_SECURITY_ACCESS.md` is
#: **binding**, and it sat outside the check built to stop exactly this. A
#: mechanical check that excludes the most authoritative document is a check
#: aimed at the wrong target.
#:
#: They are wider in scope than F8-02a, and that is intended — a dangling test
#: name is a defect wherever it is, and these documents are read by everyone.
PROSE_FILES: tuple[Path, ...] = (
    Path(__file__),
    README,
    DOCKERFILE,
    BUILD_SCRIPT,
    ENTRYPOINT,
    REPO_ROOT / "02_ARCHITECTURE.md",
    REPO_ROOT / "03_SECURITY_ACCESS.md",
)

_TEST_NAME = re.compile(r"\btest_[a-z0-9_]+")


def test_every_test_named_in_prose_actually_exists() -> None:
    """No comment, docstring or document may name a test that does not exist.

    This is a check on a habit rather than on the product, and it is here
    because the habit has now failed in four review rounds across two tickets.
    The specific case that prompted it: the comment on
    `PUBLIC_TRUST_STORE_PREFIXES` — the T7 control's core constant — cited
    a test whose name then ended `..._are_the_public_trust_store`, after it had
    been renamed, and `_is_private_key_shaped`, which never existed. (Both are
    written here without their `test_` prefix on purpose — naming a dead test in
    full, even to describe it, is what this check exists to forbid.)

    **The bound, and it is the reason the file list matters more than the
    regex.** This finds a named test that does not exist. It cannot find a
    property asserted with *no* test named, or a paragraph that is simply out of
    date — round 3 failed on a paragraph in `03_SECURITY_ACCESS.md` that named
    three real tests, all of them for the wrong thing. Nothing mechanical here
    replaces reading the anchor documents against the code.

    The convention this codebase relies on is that every asserted security
    property names the test that fails if it is violated. A named test that does
    not exist quietly converts that convention into decoration, and it is
    exactly what the next reader acts on.
    """

    defined = {
        node.name
        # `rglob`, not `glob`: F9-01 added `tests/integration/`, and a
        # non-recursive search reported that directory's real tests as
        # nonexistent. The search must cover every directory tests live in.
        for path in sorted((REPO_ROOT / "services" / "api" / "tests").rglob("test_*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }
    assert len(defined) > 100, f"only {len(defined)} tests discovered — reading the wrong tree"

    modules = {path.stem for path in (REPO_ROOT / "services" / "api" / "tests").rglob("test_*.py")}

    referenced: dict[str, str] = {}
    for path in PROSE_FILES:
        for name in _TEST_NAME.findall(path.read_text()):
            if name not in modules:
                referenced.setdefault(name, path.name)
    assert referenced, "no test names found in prose — this check is reading nothing"

    dangling = {name: where for name, where in referenced.items() if name not in defined}
    assert not dangling, (
        "prose names tests that do not exist (renamed? never written? wrapped across "
        f"two lines?): {dangling}"
    )


def test_the_readme_states_the_real_number_of_tests() -> None:
    """A count in prose goes stale silently. This is the cheapest way to stop that.

    Round 2 of the review found the README claiming 33 tests when there were 41.
    Counts test *functions*; pytest collects more cases than that because several
    are parametrised, and conflating the two is its own small drift.
    """

    defined = [
        node.name
        for node in ast.walk(ast.parse(Path(__file__).read_text()))
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    stated = re.search(
        r"`services/api/tests/test_workload_image\.py`, (\d+) test functions", README.read_text()
    )
    assert stated is not None, "the README no longer states a test count in the expected form"
    assert int(stated.group(1)) == len(defined), (
        f"README says {stated.group(1)} tests, the file defines {len(defined)}"
    )


def test_the_build_script_refuses_to_push_a_test_context(tmp_path: Path) -> None:
    """`--push` must publish what the repository builds, never a synthetic context."""

    _require_docker_or_skip()
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(BUILD_SCRIPT), "--push"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "MCPFORGE_BUILD_CONTEXT": str(tmp_path),
            "MCPFORGE_BUILD_OUTPUT": str(tmp_path / "oci"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode != 0
    assert "refuses to run against MCPFORGE_BUILD_OUTPUT" in result.stderr, result.stderr


# --- the T7 control -------------------------------------------------------


class LayerMember(NamedTuple):
    """One tar member of one layer, plus what was read out of it.

    Content is read only for members whose *name* is credential-shaped — a few
    hundred files rather than a few thousand — because it is consulted solely to
    decide whether a name match may be exempted.

    Two fields rather than one, and the asymmetry is the point:

    - `head` is the first `MAX_INSPECTED_BYTES`, used to confirm the member
      looks like a certificate. `None` means nothing was read, which is never
      exemptible.
    - `private_key_marker` is computed over the **entire** member by streaming.
      A file can be a legitimate CA bundle for its first 64 KiB and carry a
      private key at byte 200,000; two real exempted members in this image are
      larger than the cap, so this is a live case, not a hypothetical.
    """

    layer: str
    name: str
    linkname: str
    head: bytes | None
    private_key_marker: bool


def read_member_content(stream: IO[bytes]) -> tuple[bytes, bool]:
    """Return (first `MAX_INSPECTED_BYTES`, whether a private-key marker appears anywhere).

    Streamed in `STREAM_CHUNK_BYTES` chunks with a carry of `len(marker) - 1`
    bytes, so a marker straddling a chunk boundary is still found and memory
    stays bounded however large the member is.
    """

    head = b""
    carry = b""
    found = False
    while True:
        chunk = stream.read(STREAM_CHUNK_BYTES)
        if not chunk:
            break
        if len(head) < MAX_INSPECTED_BYTES:
            head += chunk[: MAX_INSPECTED_BYTES - len(head)]
        if not found and PEM_PRIVATE_KEY_MARKER in carry + chunk:
            found = True
        carry = chunk[-(len(PEM_PRIVATE_KEY_MARKER) - 1) :]
    return head, found


def _layer_members(digest: str) -> list[LayerMember]:
    """Every member of one layer blob, read from the blob itself.

    Not `docker history`, not the Dockerfile, not a squashed filesystem: the
    actual tar members of the actual layer, because a file deleted in a later
    layer is still present in the earlier one and is still inside the attested
    artefact.
    """

    members: list[LayerMember] = []
    with tarfile.open(_blob_path(digest), mode="r:*") as archive:
        for info in archive:
            head: bytes | None = None
            private_key_marker = False
            if info.isfile() and _is_credential_shaped(info.name):
                extracted = archive.extractfile(info)
                if extracted is not None:
                    head, private_key_marker = read_member_content(extracted)
            members.append(
                LayerMember(
                    layer=digest,
                    name=info.name,
                    linkname=info.linkname,
                    head=head,
                    private_key_marker=private_key_marker,
                )
            )
    return members


def _all_layer_members() -> list[LayerMember]:
    index = json.loads((OCI_LAYOUT / "index.json").read_text())
    manifest = _read_blob_json(index["manifests"][0]["digest"])
    members: list[LayerMember] = []
    for layer in manifest["layers"]:
        members.extend(_layer_members(layer["digest"]))
    return members


def _all_layer_member_names() -> list[str]:
    return [member.name for member in _all_layer_members()]


def _is_credential_shaped(name: str) -> bool:
    parts = [part.casefold() for part in Path(name).parts]
    if not parts:
        return False
    basename = parts[-1]
    if basename in CREDENTIAL_BASENAMES:
        return True
    if basename.startswith(".env."):
        return True
    if basename.endswith(CREDENTIAL_SUFFIXES):
        return True
    return bool(CREDENTIAL_DIRECTORY_NAMES.intersection(parts))


def _is_certificate_shaped(name: str) -> bool:
    """A trust-store entry is a certificate. `id_rsa` is not one, wherever it sits."""

    return Path(name).name.casefold().endswith(CERTIFICATE_SUFFIXES)


def _under_trust_store(name: str) -> bool:
    lowered = name.casefold().removeprefix("./")
    return lowered in PUBLIC_TRUST_STORE_FILES or lowered.startswith(PUBLIC_TRUST_STORE_PREFIXES)


def is_public_trust_store_entry(member: LayerMember) -> bool:
    """Whether this member may be exempted from the credential scan.

    Every condition must hold, and the order is the point — round 1 of this
    ticket exempted on **location alone**, so an ADC file, a `.env` and an
    `id_rsa` parked in `etc/ssl/certs/` all reported clean, and a `.pem`
    holding a private key was caught by no test at all.

    1. The **name** must be certificate-shaped. Checked before location, so
       `etc/ssl/certs/id_rsa` never reaches the location rule.
    2. The **location** must be a published CA bundle directory or one of two
       exact files.
    3. A symlink is exempt only if its target is itself certificate-shaped and
       inside the trust store. A symlink carries no content of its own, so
       there is nothing else to check and nothing it can hide.
    4. Otherwise the **content** must actually be a certificate: a PEM
       certificate block must appear in the first `MAX_INSPECTED_BYTES`, and a
       PEM private-key block must appear **nowhere in the member at all**. The
       second half is deliberately not subject to the read cap — see
       `LayerMember`. A file we could not read is never exempted.
    """

    if not _is_certificate_shaped(member.name):
        return False
    if not _under_trust_store(member.name):
        return False

    if member.linkname:
        target = os.path.normpath(
            os.path.join(os.path.dirname(member.name), member.linkname)
        ).lstrip("/")
        return _is_certificate_shaped(target) and _under_trust_store(target)

    if member.head is None:
        return False
    if member.private_key_marker:
        return False
    return PEM_CERTIFICATE_MARKER in member.head


def scan_layer_members(members: list[LayerMember]) -> tuple[list[str], list[str]]:
    """Return (offenders, exempted).

    Factored out so that a planted file can prove the scan fires, and so the
    exemptions can be asserted rather than assumed.
    """

    offenders: list[str] = []
    exempted: list[str] = []
    for member in members:
        if not _is_credential_shaped(member.name):
            continue
        if is_public_trust_store_entry(member):
            exempted.append(member.name)
        else:
            offenders.append(member.name)
    return offenders, exempted


def credential_offenders(members: list[LayerMember]) -> list[str]:
    return scan_layer_members(members)[0]


def test_no_layer_contains_credential_material(image_manifest: dict[str, Any]) -> None:
    """T7. Nothing credential-shaped in **any** layer of the attested image.

    Layer by layer rather than by the final filesystem, because `RUN rm` does
    not remove anything: the file stays in the layer it was added to, and the
    layer is part of the digest the attestation covers. The reviewer confirmed
    this directly: a planted `id_rsa` deleted by a later `RUN` was still
    reported.

    **What this scan does not catch, stated because presenting it without the
    bound would overclaim it.** It matches *paths*, and consults content only to
    decide whether a name-based match may be exempted as a public certificate.
    It therefore does not catch a credential in a file with an innocuous name
    (`appconfig.json`), a credential inside a nested archive (`secrets.tar.gz`),
    or one reached through a symlink whose own name is unremarkable. Those are
    inherent to a name scan. The guarantee is: a credential-shaped **name** in
    any layer, including a layer whose file a later layer deletes, is reported —
    and the exemption cannot be used to smuggle one, which is what
    `test_a_credential_hidden_in_the_trust_store_is_still_caught` demonstrates.
    """

    layers = image_manifest["layers"]
    assert len(layers) >= 3, f"only {len(layers)} layers — is this the right image?"

    inspected = 0
    all_members: list[LayerMember] = []
    for layer in layers:
        all_members.extend(_layer_members(layer["digest"]))
        inspected += 1

    assert inspected == len(layers)
    # Self-guard: a scan that read nothing reports clean. This is the failure
    # mode that made a Phase 3 CI check green while enforcing nothing.
    assert len(all_members) > 1000, f"only {len(all_members)} paths read — the scan is not working"
    assert any(member.name.endswith("entrypoint.py") for member in all_members), (
        "the workload entrypoint was not found in any layer — wrong artefact"
    )

    offenders, exempted = scan_layer_members(all_members)
    assert not offenders, f"credential-shaped files inside the attested image: {sorted(offenders)}"
    # Second self-guard: the exemption is what carries the risk of a false
    # negative, so it is asserted to be doing something rather than left
    # implicit. If the base image stops shipping a trust store this drops to
    # zero and this fails, which is a prompt to re-read the exemption.
    assert exempted, "no trust-store files found; the exemption rule may be matching nothing"


def test_the_only_exempted_files_are_real_public_certificates(
    image_manifest: dict[str, Any],
) -> None:
    """Every exemption is a certificate-named, trust-store-located, certificate-*bodied* file.

    Round 1 of this ticket exempted on location alone, and the reviewer showed
    that `etc/ssl/certs/.env`, `etc/ssl/certs/id_rsa`,
    `etc/ssl/certs/application_default_credentials.json` and a `.pem` holding a
    private key all reported clean. So all three conditions are asserted here on
    the real exempted set, not just the two cheap ones.
    """

    del image_manifest
    members = _all_layer_members()
    _offenders, exempted = scan_layer_members(members)
    assert exempted, "nothing exempted — this test would pass vacuously"

    by_name = {member.name: member for member in members}
    for name in exempted:
        assert _is_certificate_shaped(name), f"exempted a non-certificate name: {name}"
        assert _under_trust_store(name), f"exempted a file outside the trust store: {name}"
        member = by_name[name]
        if member.linkname:
            continue
        assert member.head is not None, f"exempted a file whose content was not read: {name}"
        assert PEM_CERTIFICATE_MARKER in member.head, f"exempted a non-certificate: {name}"
        # Computed over the whole member, not the read cap. Two real exempted
        # files here are larger than the cap.
        assert not member.private_key_marker, f"exempted a file containing a private key: {name}"


def _synthetic(name: str, data: bytes = b"") -> LayerMember:
    """A member as the reader would have produced it, including the streamed flag."""

    if not data:
        return LayerMember(
            layer="test", name=name, linkname="", head=None, private_key_marker=False
        )
    head, private_key_marker = read_member_content(io.BytesIO(data))
    return LayerMember(
        layer="test",
        name=name,
        linkname="",
        head=head,
        private_key_marker=private_key_marker,
    )


def test_a_credential_hidden_in_the_trust_store_is_still_caught() -> None:
    """The exemption cannot be used as a hiding place. This is the round-1 defect.

    Every one of these was reported **exempt** by the round-1 scan, which
    matched on location alone. The `.pem` case is the one no test could see: a
    private key wearing the canonical certificate extension, parked in the
    canonical certificate directory.
    """

    # Both PEM headers are assembled at runtime. A committed literal of the
    # private-key header would trip the CI credential scan, from which tests
    # are deliberately not exempt.
    private_key_pem = b"-----BEGIN RSA " + PEM_PRIVATE_KEY_MARKER + b"\nnot-a-real-key\n"
    certificate_pem = b"-----" + PEM_CERTIFICATE_MARKER + b"\nnot-a-real-certificate\n"

    locations = [*PUBLIC_TRUST_STORE_PREFIXES, "etc/ssl/certs/mozilla/"]
    planted: list[LayerMember] = []
    for location in locations:
        # (a) certificate name, certificate location, private-key body.
        planted.append(_synthetic(f"{location}leaked.pem", private_key_pem))
        planted.append(_synthetic(f"{location}leaked.crt", private_key_pem))
        # (b) certificate location, credential name.
        planted.append(_synthetic(f"{location}id_rsa", b"x"))
        planted.append(_synthetic(f"{location}.env", b"x"))
        planted.append(_synthetic(f"{location}application_default_credentials.json", b"x"))
        planted.append(_synthetic(f"{location}service-account.json", b"x"))
        planted.append(_synthetic(f"{location}.aws/credentials", b"x"))
        # (c) certificate name and location, but not readable and not a
        #     certificate body. Unreadable is never exempt.
        planted.append(_synthetic(f"{location}empty.pem", b""))
        planted.append(_synthetic(f"{location}random.pem", b"just some text"))
        # (d) key-shaped suffixes, which never become certificate-shaped.
        for suffix in PRIVATE_KEY_SUFFIXES:
            planted.append(_synthetic(f"{location}leaked{suffix}", certificate_pem))

    offenders, exempted = scan_layer_members(planted)
    assert not exempted, f"the exemption let credential material through: {sorted(exempted)}"
    assert sorted(offenders) == sorted(member.name for member in planted)


def test_a_private_key_past_the_read_cap_is_still_caught() -> None:
    """A CA bundle is appendable. The read cap must not become a place to append to.

    `etc/ssl/certs/ca-certificates.crt` and pip's vendored `cacert.pem` are both
    larger than `MAX_INSPECTED_BYTES` and both sit in the exemption. When the
    private-key check used the capped prefix, appending a key past the cap was
    exempted — measured by the reviewer on the real file. The marker is now
    sought over the whole member, so the position of the key is irrelevant.
    """

    certificate_pem = b"-----" + PEM_CERTIFICATE_MARKER + b"\nnot-a-real-certificate\n"
    private_key_pem = b"-----BEGIN RSA " + PEM_PRIVATE_KEY_MARKER + b"\nnot-a-real-key\n"

    # A bundle that is a genuine certificate file for far longer than the cap,
    # with the key appended well beyond it — the shape of a real CA bundle.
    padding = b"# filler line to grow the bundle past the read cap\n" * 8_000
    bundle = certificate_pem + padding + private_key_pem
    assert len(bundle) > MAX_INSPECTED_BYTES * 4, "the fixture is not larger than the cap"
    assert PEM_PRIVATE_KEY_MARKER not in bundle[:MAX_INSPECTED_BYTES], (
        "the key must sit past the cap or this test proves nothing"
    )

    head, private_key_marker = read_member_content(io.BytesIO(bundle))
    assert len(head) == MAX_INSPECTED_BYTES
    assert private_key_marker, "the whole-member scan missed a key past the read cap"

    for location in PUBLIC_TRUST_STORE_PREFIXES:
        offenders, exempted = scan_layer_members([_synthetic(f"{location}bundle.crt", bundle)])
        assert not exempted, f"a key past the read cap was exempted under {location}"
        assert offenders == [f"{location}bundle.crt"]


def test_a_marker_split_across_a_stream_chunk_boundary_is_still_found() -> None:
    """The streaming carry, which is the part of the fix that is easy to get wrong.

    Without a carry of `len(marker) - 1` bytes, a marker straddling two chunk
    reads is invisible. The offsets below place the marker across the boundary
    one byte at a time.
    """

    private_key_pem = b"-----BEGIN RSA " + PEM_PRIVATE_KEY_MARKER
    for offset in range(1, len(PEM_PRIVATE_KEY_MARKER) + 1):
        prefix = b"c" * (STREAM_CHUNK_BYTES - len(private_key_pem) + offset)
        _head, found = read_member_content(io.BytesIO(prefix + private_key_pem + b"\ntail\n"))
        assert found, f"marker split at offset {offset} was missed"


def test_the_real_image_has_exempted_members_larger_than_the_read_cap(
    image_manifest: dict[str, Any],
) -> None:
    """The blind spot above is live in this image, not theoretical.

    If this ever stops being true the whole-member scan is still correct, but
    the reason it exists would no longer be visible to a reader — so it is
    asserted rather than described.
    """

    del image_manifest
    _offenders, exempted = scan_layer_members(_all_layer_members())
    by_name = {member.name: member for member in _all_layer_members()}
    oversized = [
        name
        for name in exempted
        if by_name[name].head is not None and len(by_name[name].head or b"") == MAX_INSPECTED_BYTES
    ]
    assert oversized, (
        "no exempted member reaches the read cap; the whole-member scan may be "
        "guarding nothing in this image"
    )


def test_a_genuine_public_certificate_is_still_exempted() -> None:
    """The counterpart to the test above, so it is not passing by refusing everything.

    A scan that exempts nothing would satisfy every assertion in
    `test_a_credential_hidden_in_the_trust_store_is_still_caught` while making
    `test_no_layer_contains_credential_material` fail on 154 legitimate files.
    """

    certificate_pem = b"-----" + PEM_CERTIFICATE_MARKER + b"\nnot-a-real-certificate\n"
    genuine = [
        _synthetic("etc/ssl/certs/Amazon_Root_CA_1.pem", certificate_pem),
        _synthetic("usr/share/ca-certificates/mozilla/GTS_Root_R1.crt", certificate_pem),
        LayerMember(
            layer="test",
            name="usr/lib/ssl/cert.pem",
            linkname="/etc/ssl/certs/ca-certificates.crt",
            head=None,
            private_key_marker=False,
        ),
    ]
    offenders, exempted = scan_layer_members(genuine)
    assert not offenders, offenders
    assert sorted(exempted) == sorted(member.name for member in genuine)


def test_a_symlink_out_of_the_trust_store_is_not_exempted() -> None:
    """A symlink is exempt only if its target is itself a trust-store certificate."""

    escaping = [
        LayerMember(
            layer="test",
            name="etc/ssl/certs/sneaky.pem",
            linkname="../../../home/mcpforge/.ssh/id_rsa",
            head=None,
            private_key_marker=False,
        ),
        LayerMember(
            layer="test",
            name="etc/ssl/certs/sneaky2.pem",
            linkname="/opt/mcpforge/.env",
            head=None,
            private_key_marker=False,
        ),
    ]
    offenders, exempted = scan_layer_members(escaping)
    assert not exempted, exempted
    assert len(offenders) == 2


def test_the_two_credential_lists_have_not_diverged() -> None:
    """The runtime list in `entrypoint.py` must be a subset of this file's.

    The duplication is deliberate — two independent controls, so one careless
    edit cannot disable both. Silent *divergence* is a different thing: round 1
    had `.crt` in the entrypoint and not here, which made every `.crt` in every
    layer invisible to the layer scan and left one assertion branch unreachable.
    The lists are read out of `entrypoint.py`'s own AST rather than from recall,
    for the reason recorded in `F8-01`: a list written from memory missed `nbf`
    twice.

    Subset, not equality: this scan may be stricter than the runtime one, and
    is. The runtime scan reads a live filesystem on every start; this one reads
    a built artefact once.
    """

    module = ast.parse(ENTRYPOINT.read_text())
    literals: dict[str, set[str]] = {}
    for node in module.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names or node.value is None:
            continue
        expression = node.value
        # `frozenset({...})` is a Call, not a literal. Unwrapping it is required
        # rather than optional: without it this check reads nothing for
        # CREDENTIAL_FILENAMES and passes vacuously, which is what it did on the
        # first run and what the `name in literals` assertion below caught.
        if (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Name)
            and expression.func.id in {"frozenset", "set", "tuple", "list"}
            and len(expression.args) == 1
        ):
            expression = expression.args[0]
        try:
            value = ast.literal_eval(expression)
        except ValueError:
            continue
        if isinstance(value, frozenset | set | tuple | list):
            literals[names[0]] = {item for item in value if isinstance(item, str)}

    for name, ours in (
        ("CREDENTIAL_FILENAMES", set(CREDENTIAL_BASENAMES)),
        ("CREDENTIAL_SUFFIXES", set(CREDENTIAL_SUFFIXES)),
    ):
        assert name in literals, (
            f"{name} was not found in entrypoint.py by AST; the check is reading nothing"
        )
        theirs = literals[name]
        assert theirs, f"{name} in entrypoint.py parsed as empty"
        missing = theirs - ours
        assert not missing, (
            f"{name} has diverged: entrypoint.py matches {sorted(missing)} and the layer "
            "scan does not, so those files would be invisible to the T7 control"
        )


def test_the_layer_scan_catches_a_planted_credential(built_image: str, tmp_path: Path) -> None:
    """Prove the T7 control can fail, by building an image that violates it.

    Without this the passing tests above are assertions that a list is empty,
    which is also what a broken scan returns. Here a real derived image with a
    real `.env` in a real layer is built and exported, and the same function
    that passes above is required to report it.
    """

    del built_image
    context = tmp_path / "context"
    context.mkdir()
    # Deliberately not a real credential: the CI credential scanner reads
    # tracked files, and this one is created at runtime and never committed.
    (context / "planted.env").write_text("PLANTED=not-a-real-value\n")
    (context / "Dockerfile").write_text(
        f"FROM {LOCAL_TAG}\nCOPY --chown=10001:10001 planted.env /opt/mcpforge/.env\n"
    )

    out = tmp_path / "oci"
    build = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [  # noqa: S607
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/amd64",
            "--provenance=false",
            "--sbom=false",
            "--output",
            f"type=oci,dest={out},tar=false",
            str(context),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert build.returncode == 0, build.stderr[-3000:]

    index = json.loads((out / "index.json").read_text())
    manifest = json.loads(
        (out / "blobs" / "sha256" / index["manifests"][0]["digest"].split(":")[1]).read_text()
    )
    planted_members: list[LayerMember] = []
    for layer in manifest["layers"]:
        blob = out / "blobs" / "sha256" / layer["digest"].split(":")[1]
        with tarfile.open(blob, mode="r:*") as archive:
            for info in archive:
                planted_members.append(
                    LayerMember(
                        layer=layer["digest"],
                        name=info.name,
                        linkname=info.linkname,
                        head=None,
                        private_key_marker=False,
                    )
                )

    offenders = credential_offenders(planted_members)
    assert any(name.endswith(".env") for name in offenders), (
        "the layer scan did not report a .env that is demonstrably in a layer; "
        f"it found {offenders!r}"
    )


# ---------------------------------------------------------------------------
# 3. Behavioural — the entrypoint refuses rather than defaulting
# ---------------------------------------------------------------------------


def _run_entrypoint_on_host(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run `entrypoint.py` as a plain subprocess with a controlled environment.

    A bare environment, so a value the developer happens to have exported cannot
    satisfy a requirement the test intends to be missing.
    """

    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(ENTRYPOINT)],
        env={"PATH": os.environ.get("PATH", ""), **environment},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.parametrize(
    "absent",
    ["MCPFORGE_RUN_ID", "MCPFORGE_ATTESTATION_AUDIENCE"],
)
def test_the_entrypoint_refuses_when_a_required_value_is_absent(absent: str) -> None:
    """Acceptance: refuses to start when configuration is absent.

    One value is removed at a time from a configuration that is otherwise
    complete, so the refusal is attributable to that removal and not to some
    other gap.
    """

    environment = dict(GOOD_CONFIG)
    del environment[absent]

    result = _run_entrypoint_on_host(environment)
    assert result.returncode == EXIT_CONFIG_MISSING, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reason"] == "CONFIG_MISSING"
    assert absent in payload["detail"]


def test_a_blank_required_value_counts_as_absent() -> None:
    """`-e VAR=` is how an operator blanks a value. It must not be a default."""

    environment = dict(GOOD_CONFIG)
    environment["MCPFORGE_ATTESTATION_AUDIENCE"] = "   "

    result = _run_entrypoint_on_host(environment)
    assert result.returncode == EXIT_CONFIG_MISSING, result.stdout + result.stderr


@pytest.mark.parametrize("value", ["/", "", "/workspace"])
def test_the_entrypoint_refuses_any_attempt_to_move_the_jail(value: str) -> None:
    """`MCPFORGE_WORKSPACE_ROOT` is not configuration any more; present is a refusal.

    Even the right value, and even blank: a launch that still sets it expects to
    control the jail, and is told it cannot rather than silently ignored.
    """

    result = _run_entrypoint_on_host({**GOOD_CONFIG, "MCPFORGE_WORKSPACE_ROOT": value})
    assert result.returncode == EXIT_CONFIG_INVALID, result.stdout + result.stderr
    assert "MCPFORGE_WORKSPACE_ROOT" in json.loads(result.stdout)["detail"]


def test_the_complete_configuration_passes_preflight_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative tests above are only meaningful if the positive one passes.

    In process rather than as a subprocess, because the jail root is now the
    constant `/workspace`, which a developer's machine does not have;
    `preflight` takes the root as a parameter for exactly this, and `main`
    always passes the constant. The attestation step is exercised in
    `test_confidential_space.py` and, in the real image, below.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    jail = tmp_path / "jail"
    jail.mkdir()

    config, record = load_entrypoint().preflight(dict(GOOD_CONFIG), workspace_root=jail)

    assert config == GOOD_CONFIG
    assert record["workspace_root"] == str(jail)
    assert record["workload"] == (
        "mcpforge.execution.confidential_space.ConfidentialSpaceSecureExecutor"
    )
    assert record["job_runner_present"] is False
    assert "trust_level" not in record, "preflight verifies nothing, so it reports no trust level"


@pytest.mark.parametrize(
    ("audience", "why"),
    [
        ("short", "a placeholder rather than a nonce"),
        ("  1f0c7d2a9b4e6f81c3a5  ", "padded; the audience is compared without normalisation"),
    ],
)
def test_the_entrypoint_refuses_an_audience_that_is_not_a_usable_nonce(
    audience: str, why: str
) -> None:
    """The audience is what stops an attestation token being replayed.

    `attestation.py` compares it byte for byte and refuses a token carrying more
    than one audience, so a padded or placeholder value here would produce a
    token that could never verify — or, worse, one that verified against a value
    an attacker could also guess.
    """

    environment = dict(GOOD_CONFIG)
    environment["MCPFORGE_ATTESTATION_AUDIENCE"] = audience

    result = _run_entrypoint_on_host(environment)
    assert result.returncode == EXIT_CONFIG_INVALID, f"{why} was accepted: {result.stdout}"


@pytest.mark.parametrize(
    ("run_id", "why"),
    [
        ("../escape", "path traversal"),
        ("run/with/slashes", "path separators"),
        (".hidden", "leading dot"),
        ("a" * 200, "unbounded length"),
    ],
)
def test_the_entrypoint_refuses_a_run_id_that_is_not_a_safe_path_component(
    run_id: str, why: str
) -> None:
    environment = dict(GOOD_CONFIG)
    environment["MCPFORGE_RUN_ID"] = run_id

    result = _run_entrypoint_on_host(environment)
    assert result.returncode == EXIT_CONFIG_INVALID, f"{why} was accepted: {result.stdout}"


def test_the_entrypoint_refuses_key_file_adc(tmp_path: Path) -> None:
    """03_SECURITY_ACCESS.md §9: key-file ADC is not a supported configuration."""

    environment = dict(GOOD_CONFIG)
    environment["HOME"] = str(tmp_path)
    environment["GOOGLE_APPLICATION_CREDENTIALS"] = "/nonexistent/key.json"

    result = _run_entrypoint_on_host(environment)
    assert result.returncode == EXIT_CREDENTIAL_PRESENT, result.stdout + result.stderr
    assert json.loads(result.stdout)["reason"] == "CREDENTIAL_PRESENT"


def test_the_entrypoint_refuses_an_unusable_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    entrypoint = load_entrypoint()

    with pytest.raises(entrypoint.RefusalError) as caught:
        entrypoint.preflight(dict(GOOD_CONFIG), workspace_root=tmp_path / "does-not-exist")

    assert caught.value.code == EXIT_WORKSPACE_UNUSABLE


# --- the same refusals, in the real container ------------------------------


def test_the_container_passes_preflight_and_refuses_without_a_launcher(built_image: str) -> None:
    """The image as shipped, with the two relying-party-issued values and no launcher.

    Plain Docker has no Confidential Space launcher, so no token can be
    obtained: the image must get through every preflight check and then refuse
    at exactly the attestation step, rather than start. The preflight record
    rides along in the refusal so this is visible. The workload names no trust
    level in any outcome: only the relying party's verification can.
    """

    result = _docker_run(built_image, dict(GOOD_CONFIG))
    assert result.returncode == EXIT_ATTESTATION_UNAVAILABLE, result.stdout + result.stderr
    assert result.stdout.count("\n") == 1, "stdout must be one JSON line; logs go to stderr"
    payload = json.loads(result.stdout)
    assert payload["reason"] == "ATTESTATION_UNAVAILABLE"
    assert payload["attestation"]["retrieval_failure"] == "SOCKET_UNAVAILABLE"
    assert payload["attestation"]["token_obtained"] is False
    assert payload["attestation"]["delivered_to"] is None
    assert "trust_level" not in payload["attestation"], "the workload claims nothing"
    assert "HARDWARE_ATTESTED" not in result.stdout + result.stderr
    preflight = payload["preflight"]
    assert preflight["uid"] == 10001, "the container is not running as the workload user"
    assert preflight["workspace_root"] == "/workspace"
    assert preflight["workload"] == (
        "mcpforge.execution.confidential_space.ConfidentialSpaceSecureExecutor"
    )


def test_the_container_refuses_with_no_operator_configuration(built_image: str) -> None:
    """Acceptance, in the artefact rather than in the source tree."""

    result = _docker_run(built_image, {})
    assert result.returncode == EXIT_CONFIG_MISSING, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["reason"] == "CONFIG_MISSING"
    for name in GOOD_CONFIG:
        assert name in payload["detail"]


def test_the_container_refuses_an_operator_supplied_workspace_root(built_image: str) -> None:
    """The jail cannot be moved in the artefact either — not even to `/`."""

    result = _docker_run(built_image, {**GOOD_CONFIG, "MCPFORGE_WORKSPACE_ROOT": "/"})
    assert result.returncode == EXIT_CONFIG_INVALID, result.stdout + result.stderr


def test_the_container_refuses_key_file_adc(built_image: str) -> None:
    result = _docker_run(
        built_image,
        {**GOOD_CONFIG, "GOOGLE_APPLICATION_CREDENTIALS": "/var/run/secrets/key.json"},
    )
    assert result.returncode == EXIT_CREDENTIAL_PRESENT, result.stdout + result.stderr


def test_the_container_refuses_to_run_as_root(built_image: str) -> None:
    """`docker run --user 0` is the operator override the entrypoint re-checks.

    Confidential Space does not offer this override, but the check is the reason
    a future launch-policy change or a dropped `USER` line cannot silently make
    the workload root.
    """

    result = _docker_run(built_image, dict(GOOD_CONFIG), extra_args=("--user", "0:0"))
    assert result.returncode == 12, result.stdout + result.stderr
    assert json.loads(result.stdout)["reason"] == "RUNNING_AS_ROOT"


def test_the_container_has_no_gemini_or_github_configuration(built_image: str) -> None:
    """Nothing the backend needs to reach a model or a repository is in here.

    The workload runs code in a sandbox; it does not call Gemini and does not
    hold an installation token. If either ever appears in this image it is
    inside the trust boundary and covered by the attested digest.
    """

    result = _docker_run(built_image, dict(GOOD_CONFIG))
    assert result.returncode == EXIT_ATTESTATION_UNAVAILABLE, result.stdout + result.stderr
    members = _all_layer_member_names()
    for forbidden in ("google/genai", "github", "firebase"):
        offenders = [name for name in members if f"/{forbidden}" in f"/{name}".casefold()]
        assert not offenders, f"{forbidden} material in the image: {offenders[:5]}"


def test_the_container_takes_its_token_from_the_launcher_socket(
    built_image: str, private_pem: str
) -> None:
    """The real artefact, requesting a token over a socket mounted where the
    launcher's is, then trying to deliver it.

    The container runs with no network, so the metadata server that would give
    it a storage credential is unreachable: the only acceptable result is a
    token *obtained* and *not delivered* — exit 17, `ACCESS_TOKEN_UNAVAILABLE`.
    The workload verifies nothing either way; that is the relying party's job.
    This shows the request leaves the real image in the documented shape, the
    response is accepted as a token, and the token does not reach the output.
    """

    directory = Path(tempfile.mkdtemp(prefix="mcpforge-cs-"))
    directory.chmod(0o755)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://confidentialcomputing.googleapis.com",
            "aud": GOOD_CONFIG["MCPFORGE_ATTESTATION_AUDIENCE"],
            "sub": "container-check",
            "iat": now,
            "exp": now + 600,
        },
        private_pem,
        algorithm="RS256",
    )
    socket_path = directory / "teeserver.sock"
    server = launcher_stub._LauncherServer(str(socket_path), launcher_stub.serve_token(token))
    socket_path.chmod(0o666)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        result = _docker_run(
            built_image,
            dict(GOOD_CONFIG),
            extra_args=("-v", f"{directory}:/run/container_launcher"),
        )
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(directory, ignore_errors=True)

    assert result.returncode == EXIT_DELIVERY_FAILED, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["reason"] == "DELIVERY_FAILED"
    attestation = payload["attestation"]
    assert attestation["token_obtained"] is True
    assert attestation["delivery_failure"] == "ACCESS_TOKEN_UNAVAILABLE"
    assert attestation["delivered_to"] is None
    assert "trust_level" not in attestation, "the workload verifies nothing and claims nothing"
    assert attestation["token_sha256_prefix"] == hashlib.sha256(token.encode()).hexdigest()[:16]

    assert len(server.received) == 1
    request = server.received[0]
    assert (request.method, request.path) == ("POST", "/v1/token")
    assert json.loads(request.body) == {
        "audience": GOOD_CONFIG["MCPFORGE_ATTESTATION_AUDIENCE"],
        "token_type": "OIDC",
    }
    assert launcher_stub.leaked_fragments(token, result.stdout + result.stderr) == []


# --- F8-02: the launch contract, against the built image ---------------------


def test_every_required_name_is_supplied_by_exactly_one_source(
    image_config: dict[str, Any], tmp_path: Path
) -> None:
    """Every name the entrypoint requires reaches it from exactly one place.

    Derived from code at every end rather than from a list here: the required
    names from `entrypoint.py`'s own `REQUIRED_ENVIRONMENT`; the image's
    environment and its `allow_env_override` label from the **built** image's
    config; the operator's values from the `tee-env-*` entries of the command
    `launch.sh` prints, as bash parses it. A required name supplied by neither
    source, a name supplied by both, or a `tee-env-*` name the launch policy
    does not allow (which the launcher refuses outright) each fail.

    This is the check the failed VM run needed: its workload refused because
    required configuration was missing.
    """

    required = tuple(load_entrypoint().REQUIRED_ENVIRONMENT)
    assert required, "REQUIRED_ENVIRONMENT is empty — reading the wrong file"

    image_env = {entry.partition("=")[0] for entry in image_config["config"]["Env"]}
    plan = run_launch_plan(tmp_path)
    assert plan.exit_code == 0, plan.stdout + plan.stderr
    tee_env = {key.removeprefix("tee-env-") for key in plan.metadata if key.startswith("tee-env-")}
    allowed = set(
        image_config["config"]["Labels"]["tee.launch_policy.allow_env_override"].split(",")
    )

    unsupplied = [name for name in required if name not in image_env | tee_env]
    assert unsupplied == [], (
        f"required but supplied by neither the image nor launch.sh: {unsupplied}"
    )
    assert image_env & tee_env == set(), f"supplied by both: {sorted(image_env & tee_env)}"
    assert tee_env <= allowed, f"not allowed by the launch policy: {sorted(tee_env - allowed)}"
