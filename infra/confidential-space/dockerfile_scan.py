"""Docker-faithful Dockerfile parsing, and the one build-secret keyword list.

Two callers share this module, and that is the point of it existing:

* `build.sh` runs it as a script before it builds anything.
* `services/api/tests/test_workload_image.py` imports it.

Both used to carry their own parser and their own keyword list, and the two
drifted in opposite directions until a credential could walk between them. The
measured escapes, all four of which this module exists to close:

1. A comment line ending in ``\\``. Both callers joined continuations *before*
   dropping comment lines; Docker drops comment lines **first**. So

       # anything at all \\
       ENV GEMINI_API_KEY=...

   put the variable into the image config while the test helper returned only
   ``['FROM python:3.12-slim-bookworm']`` and the shell guard printed nothing.
2. A continuation split mid-token. Python's ``replace("\\\\\\n", " ")`` joined
   with a **space**, so ``GEMINI_API_K\\`` + ``EY=`` never became one word;
   Docker and the shell guard's ``sed`` joined with **nothing**, so it did.
3. Case. The shell guard's ``grep`` was case-sensitive, so a lowercase ``env``
   instruction — which Docker accepts — matched nothing.
4. List drift. The Python list had ``API_KEY`` and ``PRIVATE_KEY`` but no bare
   ``KEY``, so ``GEMINI_KEY=`` passed it; the shell guard's ``[^#]*`` stopped at
   the first ``#`` in the joined text, so a ``#`` in an earlier ``ENV`` value
   blinded it.

**This is defence in depth and not the control.** Text parsing can only ever
catch what it can spell, and a Dockerfile can construct a variable name in ways
no parser here will follow. The control is the artefact: the built image's
``Config.Env`` is asserted **exactly**, name and value, by
`test_the_image_config_declares_exactly_the_documented_environment`, and the
final stage's recorded instruction history is asserted **exactly** by
`test_the_final_stage_ran_exactly_the_documented_instructions`. Those read the
bytes the digest actually covers, so they are spelling-proof. This module runs
earlier and fails faster.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The one keyword list. Matched case-folded against a whole instruction, so it
#: covers the variable name and the value alike. `KEY` is deliberately bare:
#: `API_KEY` and `PRIVATE_KEY` are strictly narrower, and a bare `GEMINI_KEY=`
#: escaped a list that held only the narrow forms.
SECRET_KEYWORDS: tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "key",
)

#: Instructions whose whole text is scanned for the keywords above. `ARG` and
#: `ENV` declare a value that lands in the build or in the image config, so a
#: credential-shaped one is a finding on its face.
#:
#: `RUN` is deliberately **not** keyword-scanned. Its text is full of ordinary
#: paths — this Dockerfile writes to `/etc/passwd` — and a list that produces
#: false positives is a list somebody eventually shortens. A `RUN` that writes
#: a credential writes it into a layer, which is
#: `test_no_layer_contains_credential_material`'s job, and one that exports it
#: into the environment is caught exactly by
#: `test_the_image_config_declares_exactly_the_documented_environment`.
#:
#: An explicit `--mount=type=secret` is checked on **every** instruction
#: regardless, because there is no spelling of it that is not a build secret.
KEYWORD_SCANNED_INSTRUCTIONS: tuple[str, ...] = ("ARG", "ENV")


class DockerfileParseError(Exception):
    """The Dockerfile uses a feature this parser does not model faithfully.

    Raised rather than parsed approximately: a security check that silently
    mis-parses its input is worse than one that refuses to run.
    """


def instructions(text: str) -> list[str]:
    """The Dockerfile's instructions, parsed the way Docker parses them.

    Docker's order, which this reproduces exactly:

    1. A line whose first non-whitespace character is ``#`` is a comment and is
       **removed before anything else**, including when it sits in the middle of
       a backslash-continued instruction.
    2. A line ending in ``\\`` is continued: the backslash and the newline are
       removed and the next line is appended with **nothing** between them, so a
       token may be split across the break.

    Each returned instruction then has its internal whitespace collapsed to
    single spaces. That is safe for both uses here because step 2 has already
    rejoined any split token; it happens after the join, never instead of it.

    Raises `DockerfileParseError` if the file sets the ``escape`` parser
    directive, which would change the continuation character and make every
    statement above false.
    """

    lines = text.splitlines()
    for line in lines:
        # Parser directives are comments, must precede every builder
        # instruction, and are the one thing that invalidates the rules above.
        if not line.strip():
            continue
        if not line.lstrip().startswith("#"):
            break
        if line.lstrip().removeprefix("#").strip().lower().startswith("escape="):
            raise DockerfileParseError(
                "the Dockerfile sets the `escape` parser directive; this parser "
                "models the default `\\` continuation character only"
            )

    code = [line for line in lines if not line.lstrip().startswith("#")]

    parsed: list[str] = []
    pending: list[str] = []
    for line in code:
        if line.endswith("\\"):
            pending.append(line[:-1])
            continue
        pending.append(line)
        joined = "".join(pending)
        pending = []
        if joined.strip():
            parsed.append(" ".join(joined.split()))
    if pending:
        joined = "".join(pending)
        if joined.strip():
            parsed.append(" ".join(joined.split()))
    return parsed


def verb(instruction: str) -> str:
    """The instruction keyword, upper-cased. Docker's verbs are case-insensitive."""

    head, _, _ = instruction.partition(" ")
    return head.upper()


def build_secret_findings(text: str) -> list[str]:
    """Every instruction in `text` that looks like it carries a build secret.

    Returns a human-readable line per finding, and an empty list when there is
    nothing to report. The caller decides what a finding means.
    """

    findings: list[str] = []
    for instruction in instructions(text):
        name = verb(instruction)
        folded = instruction.casefold()
        if "--mount=type=secret" in folded:
            findings.append(f"mounts a build secret: {instruction}")
        if name not in KEYWORD_SCANNED_INSTRUCTIONS:
            continue
        for keyword in SECRET_KEYWORDS:
            if keyword in folded:
                findings.append(f"secret-shaped {name} (matched {keyword!r}): {instruction}")
                break
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: dockerfile_scan.py <Dockerfile>", file=sys.stderr)
        return 2
    findings = build_secret_findings(Path(argv[1]).read_text())
    for finding in findings:
        print(f"dockerfile_scan: {finding}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
