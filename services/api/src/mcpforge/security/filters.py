"""Secret and path filtering — 03_SECURITY_ACCESS.md §4.

This is the T1/T2 control: the thing that keeps a developer's private source and
credentials out of a model prompt.

Ordering is binding. Filtering happens **before** indexing, before prompt
construction, and before anything is persisted. There is no code path in which
an unfiltered file body reaches a prompt builder.

A file with a detected secret is **excluded, not redacted**. Scrubbing a partial
match and forwarding the rest is how a secret survives filtering.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import PurePosixPath

# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------

#: Directories never read. Build output, dependencies, VCS internals.
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".next",
        ".nuxt",
        ".svelte-kit",
        "dist",
        "build",
        "out",
        "coverage",
        ".git",
        ".turbo",
        ".cache",
        "__pycache__",
        ".venv",
        "venv",
        "vendor",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "target",
        ".gradle",
    }
)

#: Files quarantined on their path alone. Never opened, never hashed, never sent.
SECRET_PATH_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p8",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "*.crt",
    "*.cer",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "service-account*.json",
    "gcp-*.json",
    "firebase-adminsdk-*.json",
    "*.tfvars",
    ".npmrc",
    ".netrc",
    ".git-credentials",
    "credentials",
    "credentials.json",
)

#: Any path containing one of these segments is quarantined.
SECRET_DIR_SEGMENTS: frozenset[str] = frozenset(
    {"secrets", "credentials", ".aws", ".ssh", ".gnupg"}
)

#: Extensions we never parse. Binary, media, archives, fonts.
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".ico",
        ".bmp",
        ".tiff",
        ".mp4",
        ".mov",
        ".avi",
        ".webm",
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".wasm",
        ".class",
        ".jar",
        ".db",
        ".sqlite",
        ".sqlite3",
    }
)

#: Lockfiles: presence is recorded, contents are not read.
LOCKFILES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
    }
)


class Verdict(StrEnum):
    INCLUDE = "INCLUDE"
    EXCLUDED_PATH = "EXCLUDED_PATH"
    EXCLUDED_BINARY = "EXCLUDED_BINARY"
    EXCLUDED_SIZE = "EXCLUDED_SIZE"
    EXCLUDED_LOCKFILE = "EXCLUDED_LOCKFILE"
    QUARANTINED_PATH = "QUARANTINED_PATH"
    QUARANTINED_CONTENT = "QUARANTINED_CONTENT"

    @property
    def is_secret(self) -> bool:
        return self in (Verdict.QUARANTINED_PATH, Verdict.QUARANTINED_CONTENT)


@dataclass(frozen=True)
class FileDecision:
    """Why a file was kept or dropped.

    `reason` never contains file content — only the rule that fired, so a
    quarantine record can be shown to the user without leaking what it found.
    """

    path: str
    verdict: Verdict
    reason: str

    @property
    def included(self) -> bool:
        return self.verdict is Verdict.INCLUDE


def _matches_secret_path(name: str) -> bool:
    """Case-insensitive on purpose.

    `.ENV`, `ID_RSA` and `Server.PEM` are the same files as their lowercase
    forms on a case-insensitive filesystem, and are credentials either way. An
    earlier version matched case-sensitively and read all three.
    """
    lowered = name.lower()
    return any(fnmatch(lowered, pattern.lower()) for pattern in SECRET_PATH_PATTERNS)


def classify_path(path: str, *, size_bytes: int, max_bytes: int = 262_144) -> FileDecision:
    """Decide on a path alone, before the file is ever opened.

    Everything here must be decidable without reading the file, because for a
    quarantined path we must never read it.
    """
    posix = PurePosixPath(path)
    parts = posix.parts
    name = posix.name
    # Every comparison below is case-folded. Matching case-sensitively meant
    # `.ENV` and `Secrets/prod.yaml` were opened and indexed.
    lowered_parts = [p.lower() for p in parts[:-1]]

    for original, segment in zip(parts[:-1], lowered_parts, strict=True):
        if segment in SECRET_DIR_SEGMENTS:
            return FileDecision(path, Verdict.QUARANTINED_PATH, f"inside '{original}/'")

    if _matches_secret_path(name):
        return FileDecision(path, Verdict.QUARANTINED_PATH, "credential filename pattern")

    for original, segment in zip(parts[:-1], lowered_parts, strict=True):
        if segment in EXCLUDED_DIRS:
            return FileDecision(path, Verdict.EXCLUDED_PATH, f"inside '{original}/'")

    if name.lower() in LOCKFILES:
        return FileDecision(path, Verdict.EXCLUDED_LOCKFILE, "lockfile; presence recorded only")

    if posix.suffix.lower() in BINARY_EXTENSIONS:
        return FileDecision(path, Verdict.EXCLUDED_BINARY, f"binary extension '{posix.suffix}'")

    if size_bytes > max_bytes:
        return FileDecision(path, Verdict.EXCLUDED_SIZE, f"{size_bytes} bytes exceeds {max_bytes}")

    return FileDecision(path, Verdict.INCLUDE, "source file")


# ---------------------------------------------------------------------------
# Content scanning
# ---------------------------------------------------------------------------

#: Known credential shapes. Each is a (name, pattern) pair so a finding can name
#: the rule that fired without quoting the match.
CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("google oauth/api credential", re.compile(r"\bAQ\.[A-Za-z0-9_\-]{30,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}")),
    ("openai key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("stripe key", re.compile(r"\b[sr]k_(live|test)_[A-Za-z0-9]{20,}\b")),
    ("jwt", re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    (
        "connection string with password",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s:/@]{4,}@[^\s/]+", re.IGNORECASE),
    ),
)

#: Variable names that suggest the value beside them is a credential.
SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?:api[_-]?key|secret|password|passwd|token|credential|private[_-]?key|
       access[_-]?key|auth[_-]?token|client[_-]?secret)\b
    \s*[:=]\s*
    ['"]?(?P<value>[A-Za-z0-9_\-+/=.]{16,})['"]?
    """
)

#: Values that look like placeholders rather than real credentials.
PLACEHOLDER = re.compile(
    r"(?i)^(?:your|my|the|some|example|sample|dummy|fake|test|placeholder|"
    r"changeme|xxx+|\.\.\.|<|\$\{|process\.env|os\.environ)"
)


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


@dataclass(frozen=True)
class ContentFinding:
    rule: str
    line: int

    def describe(self) -> str:
        """Never quotes the matched text. A finding names the rule and the line."""
        return f"{self.rule} at line {self.line}"


def scan_content(text: str, *, entropy_threshold: float = 4.0) -> list[ContentFinding]:
    """Find credential-shaped content. Returns findings, never the values."""
    findings: list[ContentFinding] = []

    for lineno, line in enumerate(text.splitlines(), 1):
        for rule, pattern in CONTENT_PATTERNS:
            if pattern.search(line):
                findings.append(ContentFinding(rule, lineno))

        match = SECRET_ASSIGNMENT.search(line)
        if match:
            value = match.group("value")
            if not PLACEHOLDER.match(value) and shannon_entropy(value) >= entropy_threshold:
                findings.append(ContentFinding("high-entropy secret assignment", lineno))

    return findings


def classify_content(path: str, text: str) -> FileDecision:
    """Second gate: a file that survived path policy is scanned before use."""
    findings = scan_content(text)
    if findings:
        rules = sorted({f.rule for f in findings})
        return FileDecision(
            path,
            Verdict.QUARANTINED_CONTENT,
            f"{len(findings)} finding(s): {', '.join(rules)}",
        )
    return FileDecision(path, Verdict.INCLUDE, "source file")
