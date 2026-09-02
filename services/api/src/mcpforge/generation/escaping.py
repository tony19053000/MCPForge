"""Escaping for generated TypeScript — the boundary that makes generation safe.

Every value interpolated into a generated file is either an identifier we have
validated, or free text a model wrote. Free text pasted raw is arbitrary code in
someone else's repository: a description ending `*/` closes the JSDoc comment
and everything after it becomes executable, and a bare `"` breaks out of a
string literal.

This module is the only place free text becomes part of a file. Nothing in the
templates interpolates a description or title directly.
"""

from __future__ import annotations

import json
import re

#: Characters that end a comment, open a template expression, or break a string.
#: Replaced rather than rejected in comments, so a legitimate description
#: mentioning `*/` still produces a readable file.
_COMMENT_TERMINATOR = re.compile(r"\*/")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def as_ts_string(value: str) -> str:
    """A double-quoted TypeScript string literal, safely escaped.

    JSON string syntax is a subset of TypeScript's, so `json.dumps` produces a
    literal that is valid TS and cannot break out: quotes, backslashes,
    newlines and control characters are all escaped.
    """
    return json.dumps(_CONTROL.sub("", value), ensure_ascii=False)


def as_comment_text(value: str) -> str:
    """Free text safe to place inside a `/** ... */` block.

    Neutralises the comment terminator and flattens newlines, so a multi-line or
    hostile description cannot escape the comment and become code.
    """
    flattened = " ".join(_CONTROL.sub("", value).splitlines())
    # A visible space, not a zero-width character: invisible characters in
    # someone else's repository are a nasty surprise, and the reader should be
    # able to see why the text looks slightly altered.
    return _COMMENT_TERMINATOR.sub("* /", flattened)


def as_json_literal(value: object, *, indent: int = 2, reindent: str = "  ") -> str:
    """A JSON literal for embedding in a TypeScript file.

    Used for the input schema. Every string inside is escaped by `json.dumps`,
    so a hostile description in a schema description field cannot break out.
    """
    rendered = json.dumps(value, indent=indent, ensure_ascii=False)
    return rendered.replace("\n", "\n" + reindent)
