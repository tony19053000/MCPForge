"""A scripted stand-in for `gcloud`, for the F8-02b idempotency tests.

`setup.sh` decides what to change by *reading* the current state and comparing
it to what it declares. Testing that decision logic needs a `gcloud` that has a
state, so this one keeps a JSON file: reads answer from it, mutating commands
write to it, and every invocation is appended to a log the test inspects.

**This is a model of gcloud, not gcloud.** It is what makes "run it twice and
the second run changes nothing" an executed test rather than a claim, and it
proves nothing about Google's actual API. Live verification is manual and is
recorded in `infra/confidential-space/README.md` with a date and an operator.

Two properties keep it honest:

* An invocation it does not recognise is recorded as ``UNKNOWN`` and exits
  non-zero. `setup.sh` reads a non-zero `describe` as "absent", so a silently
  unrecognised command would look like a resource that needs creating and the
  idempotency test would go green on nonsense. `test_no_gcloud_invocation_was_unrecognised`
  fails on any ``UNKNOWN``, and every test that runs the script asserts it.
* It never reaches the network and holds no credentials.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

#: Verbs that change something. The test asserts none of these is invoked in
#: plan mode or in `--verify` mode; this is the guard on the constraint that
#: this ticket creates no GCP resource.
MUTATING_VERBS: frozenset[str] = frozenset(
    {
        "enable",
        "disable",
        "create",
        "create-oidc",
        "update-oidc",
        "update",
        "delete",
        "undelete",
        "add-iam-policy-binding",
        "remove-iam-policy-binding",
        "set-iam-policy",
        "keys",
    }
)


def is_mutating(argv: list[str]) -> bool:
    """Whether an invocation would change state. One definition, used by both sides."""

    return any(token in MUTATING_VERBS for token in argv if not token.startswith("-"))


def load_state(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return state


def empty_state(project_number: str = "847887912263") -> dict[str, Any]:
    """A project with the APIs off and none of this ticket's resources present."""

    return {
        "project_number": project_number,
        "enabled_services": [],
        "pool": None,
        "provider": None,
        "service_account": False,
        "project_bindings": [],
        "service_account_bindings": [],
        "bucket": None,
        "bucket_bindings": [],
    }


def parse_invocation(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split an invocation into positionals and flags. The one argv parser here.

    The test suite reads recorded invocations through this same function, so the
    stub and the assertions cannot disagree about what `--attribute-condition`
    was set to.
    """

    positional: list[str] = []
    flags: dict[str, str] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--"):
            if "=" in token:
                key, value = token.split("=", 1)
                flags[key] = value
            elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                flags[token] = argv[index + 1]
                index += 1
            else:
                flags[token] = ""
        else:
            positional.append(token)
        index += 1
    return positional, flags


def _format_field(flags: dict[str, str]) -> str:
    raw = flags.get("--format", "")
    if raw.startswith("value(") and raw.endswith(")"):
        return raw[len("value(") : -1]
    return raw


def _bindings_output(bindings: list[list[str]]) -> str:
    return "".join(f"{role}\t{member}\n" for role, member in bindings)


def _handle(argv: list[str], state: dict[str, Any]) -> tuple[int, str, bool]:
    """Return ``(exit code, stdout, state changed)`` for one invocation."""

    positional, flags = parse_invocation(argv)
    field = _format_field(flags)

    match positional:
        case ["projects", "describe", _project]:
            return 0, f"{state['project_number']}\n", False

        case ["services", "list"]:
            return 0, "".join(f"{name}\n" for name in state["enabled_services"]), False

        case ["services", "enable", service]:
            if service not in state["enabled_services"]:
                state["enabled_services"].append(service)
                return 0, "", True
            return 0, "", False

        case ["iam", "workload-identity-pools", "describe", pool_id]:
            pool = state["pool"]
            if pool is None or pool["id"] != pool_id:
                return 1, "", False
            return 0, f"{pool['state']}\n", False

        case ["iam", "workload-identity-pools", "create", pool_id]:
            state["pool"] = {"id": pool_id, "state": "ACTIVE"}
            return 0, "", True

        case ["iam", "workload-identity-pools", "providers", "describe", provider_id]:
            provider = state["provider"]
            if provider is None or provider["id"] != provider_id:
                return 1, "", False
            value = {
                "state": provider["state"],
                "attributeCondition": provider["attribute_condition"],
                'attributeMapping.list(separator=",")': provider["attribute_mapping"],
                "oidc.issuerUri": provider["issuer_uri"],
            }.get(field)
            if value is None:
                return 64, "", False
            return 0, f"{value}\n", False

        case ["iam", "workload-identity-pools", "providers", ("create-oidc" | "update-oidc"), pid]:
            state["provider"] = {
                "id": pid,
                "state": "ACTIVE",
                "attribute_condition": flags.get("--attribute-condition", ""),
                "attribute_mapping": flags.get("--attribute-mapping", ""),
                "issuer_uri": flags.get("--issuer-uri", ""),
            }
            return 0, "", True

        case ["iam", "service-accounts", "describe", _email]:
            if not state["service_account"]:
                return 1, "", False
            return 0, "", False

        case ["iam", "service-accounts", "create", _account_id]:
            state["service_account"] = True
            return 0, "", True

        case ["iam", "service-accounts", "get-iam-policy", _email]:
            if not state["service_account"]:
                return 1, "", False
            return 0, _bindings_output(state["service_account_bindings"]), False

        case ["iam", "service-accounts", "add-iam-policy-binding", _email]:
            binding = [flags["--role"], flags["--member"]]
            if binding not in state["service_account_bindings"]:
                state["service_account_bindings"].append(binding)
                return 0, "", True
            return 0, "", False

        case ["projects", "get-iam-policy", _project]:
            return 0, _bindings_output(state["project_bindings"]), False

        case ["projects", "add-iam-policy-binding", _project]:
            binding = [flags["--role"], flags["--member"]]
            if binding not in state["project_bindings"]:
                state["project_bindings"].append(binding)
                return 0, "", True
            return 0, "", False

        case ["storage", "buckets", "describe", url]:
            bucket = state.get("bucket")
            if bucket is None or bucket["url"] != url:
                return 1, "", False
            document = {
                "name": url.removeprefix("gs://"),
                "storage_url": f"{url}/",
                "location": bucket["location"],
                "uniform_bucket_level_access": bucket["uniform_bucket_level_access"],
                "public_access_prevention": bucket["public_access_prevention"],
                "lifecycle_config": bucket["lifecycle_config"],
            }
            return 0, json.dumps(document) + "\n", False

        case ["storage", "buckets", ("create" | "update") as verb, url]:
            bucket = state.get("bucket") if verb == "update" else None
            if verb == "update" and (bucket is None or bucket["url"] != url):
                return 1, "", False
            bucket = dict(bucket or {"url": url, "location": "", "lifecycle_config": None})
            if "--location" in flags:
                bucket["location"] = flags["--location"].upper()
            bucket["uniform_bucket_level_access"] = "--uniform-bucket-level-access" in flags or (
                verb == "update" and bool(bucket.get("uniform_bucket_level_access"))
            )
            if "--public-access-prevention" in flags:
                bucket["public_access_prevention"] = "enforced"
            else:
                bucket.setdefault("public_access_prevention", "inherited")
            if "--lifecycle-file" in flags:
                bucket["lifecycle_config"] = json.loads(
                    Path(flags["--lifecycle-file"]).read_text(encoding="utf-8")
                )
            state["bucket"] = bucket
            return 0, "", True

        case ["storage", "buckets", "get-iam-policy", url]:
            bucket = state.get("bucket")
            if bucket is None or bucket["url"] != url:
                return 1, "", False
            return 0, _bindings_output(state.get("bucket_bindings", [])), False

        case ["storage", "buckets", "add-iam-policy-binding", url]:
            bucket = state.get("bucket")
            if bucket is None or bucket["url"] != url:
                return 1, "", False
            binding = [flags["--role"], flags["--member"]]
            bindings = state.setdefault("bucket_bindings", [])
            if binding not in bindings:
                bindings.append(binding)
                return 0, "", True
            return 0, "", False

        case _:
            return 64, "", False


def main(argv: list[str]) -> int:
    state_path = Path(os.environ["FAKE_GCLOUD_STATE"])
    log_path = Path(os.environ["FAKE_GCLOUD_LOG"])
    state = load_state(state_path)

    code, output, changed = _handle(argv, state)
    if changed:
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {
                    "argv": argv,
                    "exit": code,
                    "mutating": is_mutating(argv),
                    "recognised": code != 64,
                }
            )
            + "\n"
        )

    sys.stdout.write(output)
    if code == 64:
        sys.stderr.write(f"fake_gcloud: unrecognised invocation: {argv}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
