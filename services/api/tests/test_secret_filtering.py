"""Secret and path filtering — F3-03.

The T1/T2 control. A fixture tree with planted secrets is built, filtered, and
then checked byte-for-byte: no secret value may appear anywhere downstream.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcpforge.security.filters import (
    Verdict,
    classify_content,
    classify_path,
    scan_content,
    shannon_entropy,
)
from mcpforge.security.pipeline import filter_tree


# Planted secrets, assembled at runtime rather than written as literals.
#
# These are fake, but they are correctly shaped — which is the point, and also
# why an earlier version of this file was blocked by GitHub push protection.
# A test fixture that trips a real secret scanner does not belong in a commit,
# so the values are built here instead. Nothing is exempted from a scanner to
# make this work.
def _fake(prefix: str, body: str) -> str:
    return prefix + body


PLANTED = {
    "env_key": _fake("AIza", "SyD-1234567890abcdefghijklmnopqrstuvw"),
    "gemini_key": _fake("AQ.", "Ab8RN6K" + "x" * 40),
    "github_token": _fake("gh" + "p_", "a" * 36),
    "aws_key": _fake("AKIA", "IOSFODNN7EXAMPLE"),
    "openai_key": _fake("sk" + "-", "b" * 40),
    "stripe_key": _fake("sk" + "_live_", "c" * 30),
    "slack_token": _fake("xox" + "b-", "123456789012-abcdefghijklmnop"),
    "db_password": "postgres://admin:sup3rS3cretP4ss@db.internal:5432/prod",
    "pem_body": "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ",
    "pem_header": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "openssh_header": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "jwt": _fake("ey", "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP92K27uhbUJU1p"),
    "high_entropy": "Xk9mQ2vL8pR4tY7wZ3nB6jH1sD5fG0aC",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small tree that looks like a real Next.js app, with secrets planted."""
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "react").mkdir(parents=True)
    (tmp_path / ".next" / "static").mkdir(parents=True)
    (tmp_path / "config" / "secrets").mkdir(parents=True)
    (tmp_path / ".ssh").mkdir()

    # Legitimate source — must survive.
    (tmp_path / "src" / "app" / "page.tsx").write_text(
        "export default function Page() { return <h1>Hotel</h1>; }\n"
    )
    (tmp_path / "src" / "lib" / "booking.ts").write_text(
        "export async function createReservation(input: Input) { return db.insert(input); }\n"
    )
    (tmp_path / "package.json").write_text('{"name":"hotel","dependencies":{}}\n')

    # Quarantined by path — must never be opened.
    (tmp_path / ".env").write_text(f"GEMINI_API_KEY={PLANTED['gemini_key']}\n")
    (tmp_path / ".env.production").write_text(f"GOOGLE_KEY={PLANTED['env_key']}\n")
    (tmp_path / "server.pem").write_text(
        f"{PLANTED['pem_header']}\n{PLANTED['pem_body']}\n-----END RSA PRIVATE KEY-----\n"
    )
    (tmp_path / "firebase-adminsdk-abc.json").write_text('{"private_key":"secret"}\n')
    (tmp_path / "config" / "secrets" / "prod.yaml").write_text(f"aws: {PLANTED['aws_key']}\n")
    (tmp_path / ".ssh" / "id_rsa").write_text(PLANTED["openssh_header"] + "\n")
    (tmp_path / ".npmrc").write_text(
        f"//registry.npmjs.org/:_authToken={PLANTED['github_token']}\n"
    )

    # Quarantined by content — ordinary filename, secret inside.
    (tmp_path / "src" / "lib" / "config.ts").write_text(
        f'export const client = new Client("{PLANTED["openai_key"]}");\n'
    )
    (tmp_path / "src" / "lib" / "db.ts").write_text(
        f'const url = "{PLANTED["db_password"]}";\nexport const db = connect(url);\n'
    )
    (tmp_path / "src" / "lib" / "auth.ts").write_text(
        f'const client_secret = "{PLANTED["high_entropy"]}";\n'
    )

    # Excluded — noise, not secrets.
    (tmp_path / "node_modules" / "react" / "index.js").write_text("module.exports = {};\n")
    (tmp_path / ".next" / "static" / "chunk.js").write_text("console.log(1);\n")
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}\n')
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return tmp_path


# -- the headline guarantee -------------------------------------------------


def test_no_planted_secret_survives_the_filter(repo: Path) -> None:
    """Not one secret byte may reach anything downstream of the filter."""
    result = filter_tree(repo)
    everything_downstream = "\n".join(text for _, text in result.included)

    for name, secret in PLANTED.items():
        assert secret not in everything_downstream, f"{name} survived filtering"


def test_the_quarantine_record_leaks_nothing(repo: Path) -> None:
    """Quarantine records are shown to users. They must carry paths and rules only."""
    result = filter_tree(repo)
    record = "\n".join(f"{d.path} {d.reason}" for d in result.quarantined)

    for name, secret in PLANTED.items():
        assert secret not in record, f"{name} leaked into a quarantine record"


def test_real_source_survives(repo: Path) -> None:
    """Filtering must not be so aggressive that nothing is left to analyze."""
    result = filter_tree(repo)
    assert "src/app/page.tsx" in result.included_paths
    assert "src/lib/booking.ts" in result.included_paths
    assert "package.json" in result.included_paths


def test_a_secret_file_is_excluded_not_scrubbed(repo: Path) -> None:
    """03_SECURITY_ACCESS.md §4.3 — no partial redaction, the file is dropped."""
    result = filter_tree(repo)
    assert "src/lib/config.ts" not in result.included_paths
    assert "src/lib/config.ts" in result.quarantined_paths


# -- path policy -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        "server.pem",
        "private.key",
        "id_rsa",
        "id_ed25519",
        "firebase-adminsdk-xyz.json",
        "service-account.json",
        "terraform.tfvars",
        ".npmrc",
        ".netrc",
        "config/secrets/prod.yaml",
        ".ssh/id_rsa",
        ".aws/credentials",
        "certs/server.crt",
    ],
)
def test_credential_paths_are_quarantined(path: str) -> None:
    assert classify_path(path, size_bytes=100).verdict.is_secret, path


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/react/index.js",
        ".next/static/chunk.js",
        "dist/bundle.js",
        "build/main.js",
        "coverage/lcov.info",
        ".git/config",
        "__pycache__/mod.pyc",
        "vendor/lib.js",
    ],
)
def test_build_output_and_dependencies_are_excluded(path: str) -> None:
    decision = classify_path(path, size_bytes=100)
    assert decision.verdict is Verdict.EXCLUDED_PATH, path


def test_lockfile_presence_is_recorded_but_contents_are_not_read(repo: Path) -> None:
    result = filter_tree(repo)
    assert "package-lock.json" in result.lockfiles
    assert "package-lock.json" not in result.included_paths


def test_binary_files_are_excluded(repo: Path) -> None:
    result = filter_tree(repo)
    assert "logo.png" not in result.included_paths


def test_oversized_files_are_excluded() -> None:
    decision = classify_path("src/huge.ts", size_bytes=999_999, max_bytes=262_144)
    assert decision.verdict is Verdict.EXCLUDED_SIZE


def test_ordinary_source_is_included() -> None:
    assert classify_path("src/app/page.tsx", size_bytes=500).included


# -- content scanning ------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("google", f'const k = "{PLANTED["env_key"]}";'),
        ("gemini", f'const k = "{PLANTED["gemini_key"]}";'),
        ("github", f'token = "{PLANTED["github_token"]}"'),
        ("aws", f'AWS_ACCESS_KEY_ID = "{PLANTED["aws_key"]}"'),
        ("openai", f'client = OpenAI("{PLANTED["openai_key"]}")'),
        ("stripe", f'stripe = Stripe("{PLANTED["stripe_key"]}")'),
        ("slack", f'slack = "{PLANTED["slack_token"]}"'),
        ("jwt", f'const t = "{PLANTED["jwt"]}";'),
        ("connection string", f'DATABASE_URL = "{PLANTED["db_password"]}"'),
        ("pem", PLANTED["pem_header"]),
        ("entropy", f'api_key = "{PLANTED["high_entropy"]}"'),
    ],
)
def test_credential_shapes_are_detected(label: str, line: str) -> None:
    assert scan_content(line), f"{label} not detected"


@pytest.mark.parametrize(
    "line",
    [
        "const apiKey = process.env.GEMINI_API_KEY;",
        'password = "changeme"',
        'const token = "your-token-here"',
        'api_key: "<YOUR_API_KEY>"',
        'secret = "${VAULT_SECRET}"',
        "export function getToken() { return session.token; }",
        "// TODO: move the password to an env var",
        'const client_secret = "example-value-here"',
    ],
)
def test_ordinary_code_is_not_flagged(line: str) -> None:
    """False positives would make the filter unusable, so placeholders and env
    lookups must pass."""
    assert not scan_content(line), line


def test_a_finding_names_the_rule_and_line_but_never_the_value() -> None:
    findings = scan_content(f'const k = "{PLANTED["github_token"]}";')
    assert findings
    described = findings[0].describe()
    assert "github token" in described
    assert PLANTED["github_token"] not in described


def test_entropy_distinguishes_random_from_english() -> None:
    assert shannon_entropy(PLANTED["high_entropy"]) > 4.0
    assert shannon_entropy("aaaaaaaaaaaaaaaa") < 1.0


def test_classify_content_reports_the_rules_that_fired() -> None:
    decision = classify_content("src/x.ts", f'const k = "{PLANTED["aws_key"]}";')
    assert decision.verdict is Verdict.QUARANTINED_CONTENT
    assert "aws access key id" in decision.reason
    assert PLANTED["aws_key"] not in decision.reason


# -- ordering, the binding rule --------------------------------------------


def test_a_quarantined_path_is_never_opened(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """03_SECURITY_ACCESS.md §4.1 — filtering precedes reading, not the reverse.

    If a credential file were opened first and judged afterwards, its bytes
    would already be in the process. This asserts they never are.
    """
    opened: list[str] = []
    original = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        opened.append(self.name)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy)
    filter_tree(repo)

    for name in (".env", ".env.production", "server.pem", "id_rsa", ".npmrc"):
        assert name not in opened, f"{name} was opened despite being quarantined by path"


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    """A symlink out of the tree must not smuggle a file in."""
    (tmp_path / "repo").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret data outside the repository\n")
    (tmp_path / "repo" / "link.txt").symlink_to(outside)

    result = filter_tree(tmp_path / "repo")
    assert result.included_paths == []


# -- case folding -----------------------------------------------------------
#
# An earlier version matched paths case-sensitively, so `.ENV`, `ID_RSA` and
# `Server.PEM` were opened, read and included. On a case-insensitive filesystem
# those *are* the lowercase files. Content scanning is no backstop: a key file
# without a PEM header and a low-entropy password match no pattern.


@pytest.mark.parametrize(
    "path",
    [
        ".ENV",
        ".Env",
        ".ENV.PRODUCTION",
        ".Env.Local",
        "ID_RSA",
        "Id_Rsa",
        "ID_ED25519",
        "Server.PEM",
        "PRIVATE.KEY",
        "Service-Account.json",
        "Firebase-AdminSDK-abc.json",
        "Terraform.TFVARS",
        ".NPMRC",
        "Secrets/prod.yaml",
        "SECRETS/prod.yaml",
        "Credentials/aws.json",
        ".SSH/config",
        ".AWS/credentials",
        "config/Secrets/db.yaml",
    ],
)
def test_credential_paths_are_quarantined_whatever_their_case(path: str) -> None:
    assert classify_path(path, size_bytes=100).verdict.is_secret, path


@pytest.mark.parametrize(
    "path",
    ["NODE_MODULES/react/index.js", "Dist/bundle.js", ".NEXT/static/chunk.js", "Build/main.js"],
)
def test_build_output_is_excluded_whatever_its_case(path: str) -> None:
    assert classify_path(path, size_bytes=100).verdict is Verdict.EXCLUDED_PATH, path


@pytest.mark.parametrize("path", ["Package-Lock.json", "YARN.LOCK", "Uv.Lock"])
def test_lockfiles_are_recognised_whatever_their_case(path: str) -> None:
    assert classify_path(path, size_bytes=100).verdict is Verdict.EXCLUDED_LOCKFILE, path


def test_an_uppercase_credential_file_is_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: not merely excluded from the output, never read at all."""
    (tmp_path / ".SSH").mkdir()
    (tmp_path / ".ENV").write_text(f"KEY={PLANTED['gemini_key']}\n")
    (tmp_path / "ID_RSA").write_text("not-a-pem-header just raw key bytes\n")
    (tmp_path / ".SSH" / "config").write_text("Host prod\n  User root\n")
    (tmp_path / "app.ts").write_text("export const a = 1;\n")

    opened: list[str] = []
    original = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        opened.append(self.name)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy)
    result = filter_tree(tmp_path)

    for name in (".ENV", "ID_RSA", "config"):
        assert name not in opened, f"{name} was opened despite being a credential path"
    assert result.included_paths == ["app.ts"]


def test_a_key_file_with_no_recognisable_content_is_still_caught(tmp_path: Path) -> None:
    """Path policy has to hold on its own — content scanning cannot save this one."""
    (tmp_path / "Server.PEM").write_text("aGVsbG8gd29ybGQgdGhpcyBpcyBub3QgYSBoZWFkZXI=\n")
    (tmp_path / "app.ts").write_text("export const a = 1;\n")

    result = filter_tree(tmp_path)
    assert "Server.PEM" in result.quarantined_paths
    assert "Server.PEM" not in result.included_paths
