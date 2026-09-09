"""The one parser for the workload identity attribute condition — F8-02b.

Two callers share this module, and that is the whole reason it exists:

* ``setup.sh`` runs it as a script (``--consistency``) **before** it plans or
  applies anything, so the script refuses to run when it and ``policy.md``
  disagree.
* ``services/api/tests/test_confidential_space_setup.py`` imports it.

`F8-02a` spent three review rounds on one defect class — two implementations of
a single rule, drifting in opposite directions until a credential walked between
them — and ``dockerfile_scan.py`` is the answer that ticket arrived at. This is
the same discipline for this ticket: there is exactly one clause splitter, one
whitespace normalisation and one condition evaluator, and both the shell and the
test suite go through them.

**What this module is, and what it is not.** :func:`evaluate` is a deliberately
tiny evaluator over the *subset of CEL that our condition actually uses*:
``&&``, ``==``, ``in``, dotted attribute paths, single-quoted strings and list
literals. It is **not** Google's CEL implementation, and a passing evaluation
here is not evidence that Google's IAM evaluates the same condition the same
way. That is manual live verification, recorded in ``README.md`` with a date and
an operator, and it has not been performed.

Anything outside that subset — ``||``, ``!``, ``!=``, parentheses,
``startsWith``, ``matches``, ``contains``, a bare ``true`` — raises
:class:`UnsupportedConditionError` rather than being skipped or approximated. A
skipped clause is how a permissive condition passes a test that claims to
constrain it, so an unparsable condition is a **failure**, never a pass.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "SetupScanError",
    "UnsupportedConditionError",
    "clause_operands",
    "documented_conditions",
    "documented_roles",
    "evaluate",
    "normalise",
    "split_clauses",
]


class SetupScanError(Exception):
    """A condition or a policy document could not be read as declared."""


class UnsupportedConditionError(SetupScanError):
    """The condition uses CEL this module deliberately refuses to guess at.

    Raised rather than returning ``False`` or ignoring the clause. A condition
    this module cannot evaluate is a condition whose security property is
    unverified, and that must be loud.
    """


#: The fenced block in ``policy.md`` that holds the attribute condition, clause
#: by clause. Delimited by explicit markers rather than by heading text, because
#: a heading is prose and prose gets edited.
_POLICY_CONDITION_BEGIN = "<!-- BEGIN ATTRIBUTE CONDITION -->"
_POLICY_CONDITION_END = "<!-- END ATTRIBUTE CONDITION -->"

#: The fenced block in ``policy.md`` that enumerates the project roles the
#: workload service account is granted.
_POLICY_ROLES_BEGIN = "<!-- BEGIN PROJECT ROLES -->"
_POLICY_ROLES_END = "<!-- END PROJECT ROLES -->"

_WHITESPACE = re.compile(r"\s+")

#: A dotted path such as ``assertion.submods.container.image_digest``. Anchored,
#: so ``assertion.hwmodel.startsWith`` is a path segment named ``startsWith``
#: rather than a call — and a call would then fail on its parenthesis.
_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

#: A single-quoted CEL string with no escapes. Escapes are refused rather than
#: half-implemented.
_STRING = re.compile(r"^'([^'\\]*)'$")

_LIST = re.compile(r"^\[(.*)\]$", re.DOTALL)

#: Everything below is rejected on sight. The point is not that these are
#: unsafe in CEL — several are perfectly reasonable — but that this module
#: cannot evaluate them, and a condition it cannot evaluate must not be reported
#: as satisfying anything. ``!=`` and ``matches`` are the two that would turn a
#: digest pin into something weaker while still containing the digest text.
_FORBIDDEN_SYNTAX: tuple[tuple[str, str], ...] = (
    ("||", "disjunction"),
    ("!=", "inequality"),
    ("(", "grouping or a function call"),
    (")", "grouping or a function call"),
    ("?", "a conditional"),
    ('"', "a double-quoted string"),
    (">", "an ordering comparison"),
    ("<", "an ordering comparison"),
)


def normalise(text: str) -> str:
    """Collapse whitespace so a line break is not a semantic difference.

    The single normalisation in this ticket. ``setup.sh`` writes the condition
    on several lines for readability, ``policy.md`` quotes it clause by clause,
    and gcloud receives it as one string; all three go through here before any
    comparison.
    """

    return _WHITESPACE.sub(" ", text).strip()


def split_clauses(condition: str) -> tuple[str, ...]:
    """The condition's clauses, in order, normalised.

    Splitting on ``&&`` is only correct because :func:`_reject_unsupported`
    has already refused parentheses and disjunction, so there is no nesting for
    a top-level split to get wrong.
    """

    _reject_unsupported(condition)
    parts = [normalise(part) for part in condition.split("&&")]
    if any(not part for part in parts):
        raise SetupScanError(f"Condition has an empty clause: {condition!r}")
    if not parts:
        raise SetupScanError("Condition has no clauses")
    return tuple(parts)


def clause_operands(clause: str) -> tuple[str, str, str]:
    """``(left, operator, right)`` for one clause, or a raised error.

    A clause this module cannot decompose is refused, so a test asserting
    something about "the digest clause" can never silently be asserting it about
    nothing.
    """

    _reject_unsupported(clause)
    for operator in ("==", " in "):
        index = clause.find(operator)
        if index == -1:
            continue
        left = normalise(clause[:index])
        right = normalise(clause[index + len(operator) :])
        if not left or not right:
            raise SetupScanError(f"Clause is missing an operand: {clause!r}")
        return left, operator.strip(), right
    raise UnsupportedConditionError(
        f"Clause uses no operator this module evaluates (== or in): {clause!r}"
    )


def evaluate(condition: str, claims: dict[str, Any]) -> bool:
    """Whether ``claims`` satisfies every clause of ``condition``.

    Bound, stated because it matters: this is our evaluator over our subset, not
    Google's. It exists to prove that a *debug* token and a *wrong-digest* token
    fail the condition we actually pass to gcloud, which is a property of the
    condition's text and can be checked here. It does not prove that Google IAM
    accepts the condition at all — that is live verification.
    """

    return all(_evaluate_clause(clause, claims) for clause in split_clauses(condition))


def documented_conditions(policy_markdown: str) -> tuple[str, ...]:
    """Every attribute-condition clause written out in ``policy.md``."""

    block = _delimited(policy_markdown, _POLICY_CONDITION_BEGIN, _POLICY_CONDITION_END)
    clauses = tuple(normalise(line) for line in block.splitlines() if normalise(line))
    if not clauses:
        raise SetupScanError("policy.md documents no attribute condition clauses")
    return clauses


def documented_roles(policy_markdown: str) -> tuple[str, ...]:
    """Every project role ``policy.md`` enumerates for the workload account."""

    block = _delimited(policy_markdown, _POLICY_ROLES_BEGIN, _POLICY_ROLES_END)
    roles = tuple(normalise(line) for line in block.splitlines() if normalise(line))
    if not roles:
        raise SetupScanError("policy.md enumerates no project roles")
    for role in roles:
        if not role.startswith("roles/"):
            raise SetupScanError(f"policy.md role is not a role id: {role!r}")
    return roles


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _reject_unsupported(text: str) -> None:
    for token, description in _FORBIDDEN_SYNTAX:
        if token in text:
            raise UnsupportedConditionError(
                f"Condition uses {description} ({token!r}), which this module refuses to "
                f"evaluate rather than approximate: {text!r}"
            )


def _delimited(markdown: str, begin: str, end: str) -> str:
    start = markdown.find(begin)
    stop = markdown.find(end)
    if start == -1 or stop == -1 or stop < start:
        raise SetupScanError(f"policy.md has no {begin} … {end} block")
    inner = markdown[start + len(begin) : stop]
    return "\n".join(line for line in inner.splitlines() if not line.strip().startswith("```"))


def _evaluate_clause(clause: str, claims: dict[str, Any]) -> bool:
    left, operator, right = clause_operands(clause)
    if operator == "==":
        return _value(left, claims) == _value(right, claims)
    container = _value(right, claims)
    if not isinstance(container, list | tuple | set | frozenset):
        return False
    return _value(left, claims) in container


class _Missing:
    """A claim the token does not carry. Equal to nothing, contained in nothing."""

    def __eq__(self, other: object) -> bool:
        del other
        return False

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return "<missing claim>"


_MISSING = _Missing()


def _value(operand: str, claims: dict[str, Any]) -> Any:
    string = _STRING.match(operand)
    if string is not None:
        return string.group(1)
    listed = _LIST.match(operand)
    if listed is not None:
        return [_value(normalise(item), claims) for item in _split_list(listed.group(1))]
    if _PATH.match(operand):
        return _lookup(operand, claims)
    raise UnsupportedConditionError(f"Operand is neither a string, a list nor a path: {operand!r}")


def _split_list(inner: str) -> list[str]:
    items = [item for item in (normalise(part) for part in inner.split(",")) if item]
    if not items:
        raise SetupScanError("Condition contains an empty list literal")
    return items


def _lookup(path: str, claims: dict[str, Any]) -> Any:
    head, *rest = path.split(".")
    if head != "assertion":
        raise UnsupportedConditionError(
            f"Only `assertion` paths are evaluated; got {path!r}. A condition that reads "
            "anything else is not one this module can vouch for."
        )
    current: Any = claims
    for segment in rest:
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _consistency(policy_path: str, condition: str, roles: list[str]) -> int:
    """Compare the live values ``setup.sh`` holds against ``policy.md``."""

    policy_markdown = _read(policy_path)
    problems: list[str] = []

    script_clauses = split_clauses(condition)
    policy_clauses = documented_conditions(policy_markdown)
    for clause in script_clauses:
        if clause not in policy_clauses:
            problems.append(f"clause in setup.sh but not in policy.md: {clause}")
    for clause in policy_clauses:
        if clause not in script_clauses:
            problems.append(f"clause in policy.md but not in setup.sh: {clause}")

    script_roles = tuple(normalise(role) for role in roles if normalise(role))
    policy_roles = documented_roles(policy_markdown)
    for role in script_roles:
        if role not in policy_roles:
            problems.append(f"role granted by setup.sh but not enumerated in policy.md: {role}")
    for role in policy_roles:
        if role not in script_roles:
            problems.append(f"role enumerated in policy.md but not granted by setup.sh: {role}")

    if problems:
        print("setup_scan: setup.sh and policy.md disagree:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise SetupScanError(f"cannot read {path}: {error}") from error


def main(argv: list[str] | None = None) -> int:
    """Two modes, both used by ``setup.sh``.

    ``--normalise`` exists so that the shell does not grow a second whitespace
    rule of its own; it compares a live gcloud value against the desired one by
    passing both through :func:`normalise` here.
    """

    parser = argparse.ArgumentParser(description="setup.sh's parser, shared with the tests.")
    parser.add_argument("--consistency", metavar="POLICY_MD")
    parser.add_argument("--condition")
    parser.add_argument("--role", action="append", default=[])
    parser.add_argument("--normalise", metavar="TEXT")
    arguments = parser.parse_args(argv)
    try:
        if arguments.normalise is not None:
            print(normalise(arguments.normalise))
            return 0
        if arguments.consistency is None or arguments.condition is None:
            parser.error("--consistency and --condition are required together")
        return _consistency(arguments.consistency, arguments.condition, arguments.role)
    except SetupScanError as error:
        print(f"setup_scan: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
