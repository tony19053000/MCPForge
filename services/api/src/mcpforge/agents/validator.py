"""Agent 5 — the Validator, ticket F8-04, 02_ARCHITECTURE.md §4 and §11.

Agent 5 answers one question: does the transformed application actually work for
an agent? It answers it by running commands in the secure workspace and reading
their exit codes.

**It makes no model call.** Like agent 3, the judgement was spent earlier — the
tools and their mappings were chosen by agent 2 and approved by a human — and
what remains is execution. 02_ARCHITECTURE.md §4 already binds this: "Agent 5's
verdict is computed from real command exit codes, not from model text." So this
module holds no system instruction, no prompt builder and no provider, and does
not subclass `Agent`, which is the only base that can reach Gemini. That absence
is asserted structurally by
`test_no_module_both_prompts_gemini_and_touches_the_score`, an AST sweep over
every backend module, and by `test_the_validator_module_cannot_reach_gemini`.

What it runs, per the ticket:

| Check | Component (§11) |
|---|---|
| the tool registers and is discoverable | Tool registration & discovery |
| its input schema is valid and matches the approved contract | Schema validity |
| a valid call executes | Successful execution |
| an invalid call is rejected | Error handling |
| a gated tool refuses to act without approval | Authorization & approval safety |
| the application's own test script | Regression tests still passing |
| the application's own typecheck and build scripts | *ungraded* — §11 gives
  them no points, and inventing one would be a weight this project never
  agreed to |

**UI state synchronization has no executable check and therefore scores zero.**
MCPForge cannot observe an arbitrary application's DOM updating without a
browser and app-specific knowledge of what should change; Playwright arrives at
`F9-03`. Rather than defaulting the row, the validator plans the check, records
it as skipped with that reason, and the component scores zero. That is the
acceptance criterion — "a component with no evidence contributes zero" —
demonstrated on a real run rather than only in a unit test.

**The harness is workspace-only.** The registration and schema checks need a
file that registers the generated toolset against a stub model context. It is
written into the ephemeral workspace beside the patched application and is never
part of the `GeneratedPatch`, never committed, and never seen by the developer's
repository — asserted by `test_the_harness_is_never_part_of_the_patch`.

**Security.** Every command is an argument array from the executor's allowlist,
run in the workspace with no outbound network. Nothing here installs anything:
the workspace has no network, so the application's dependency tree must already
be present. When it is not, the checks that need it are skipped with that reason
and score zero rather than being reported as passing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from mcpforge.execution.provider import (
    Command,
    CommandResult,
    SandboxError,
    SecureExecutionProvider,
    Workspace,
    resolve_inside,
)
from mcpforge.generation.escaping import as_json_literal, as_ts_string
from mcpforge.generation.nextjs import REGISTER_PATH, tool_test_path
from mcpforge.generation.test_template import (
    AUTHORIZATION_TEST_NAME,
    EXECUTION_TEST_NAME,
    rejection_test_name,
)
from mcpforge.logging import get_logger
from mcpforge.models.webmcp import WebMCPTool, WebMCPToolset
from mcpforge.orchestration.scoring import (
    CheckEvidence,
    ExecutedCheck,
    ReadinessScore,
    ScoreComponent,
    SkippedCheck,
    score_components,
)

log = get_logger(__name__)

#: Where the harness lives, relative to the application root. Under `src/` so
#: the `@/` alias and the app's own module resolution apply, and named so it is
#: obviously not the developer's code.
HARNESS_DIR = "src/webmcp/__validation__"

#: The vitest configuration MCPForge writes. `.mts`, deliberately: a Next.js
#: `tsconfig.json` includes `**/*.ts`, so a `.ts` config and `.ts` harness would
#: be dragged into the application's own typecheck and a MCPForge type error
#: would be reported as the developer's.
HARNESS_CONFIG = "mcpforge.validation.config.mts"

#: Local CLIs, run through `node` rather than through a PATH lookup. The
#: sandbox's PATH deliberately does not include `node_modules/.bin`, and adding
#: it would be a second place that decides what may execute.
VITEST_CLI = "node_modules/vitest/vitest.mjs"

DEFAULT_CHECK_TIMEOUT_SECONDS = 300

NO_INPUTS_SKIP_REASON = (
    "The tool declares no inputs, so there is no wrong-type case to reject and "
    "the generator emits no rejection test. No evidence, so this component "
    "scores zero rather than a default."
)

UI_SYNCHRONIZATION_SKIP_REASON = (
    "MCPForge has no executable UI-synchronization check yet: observing an "
    "arbitrary application's DOM after a tool call needs a browser and "
    "app-specific expectations. This component therefore has no evidence and "
    "scores zero rather than being given a default."
)


class ValidatorError(Exception):
    """The validator could not run at all. Never a silently empty report."""


# ---------------------------------------------------------------------------
# The check suite, as data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedCheck:
    """One check: either a command to run, or a reason it cannot be run."""

    check_id: str
    component: ScoreComponent | None
    description: str
    command: Command | None = None
    skip_reason: str | None = None
    #: True when the command selects a single test by name with vitest's `-t`.
    #: Such a run exits 0 when the filter matches nothing, so its result is
    #: only evidence if a test actually ran — see `vitest_ran_a_test`. Derived
    #: from the command rather than passed in, so it cannot disagree with it.
    selects_one_test: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "selects_one_test", self.command is not None and "-t" in self.command.argv
        )
        if (self.command is None) == (self.skip_reason is None):
            raise ValueError(
                f"check {self.check_id!r} must have exactly one of a command or a skip reason"
            )


@dataclass(frozen=True)
class ApplicationFacts:
    """What the workspace actually contains. Observed, never assumed."""

    scripts: frozenset[str]
    vitest_available: bool

    @property
    def missing_vitest_reason(self) -> str:
        return (
            f"{VITEST_CLI} is not present in the application, and the validation "
            "workspace has no network to install it."
        )


def _vitest_command(
    *,
    app_dir: str,
    test_file: str,
    test_name: str | None,
    timeout_seconds: int,
) -> Command:
    argv = ["node", VITEST_CLI, "run", "--config", HARNESS_CONFIG, test_file]
    if test_name is not None:
        argv += ["-t", test_name]
    return Command(
        argv=tuple(argv),
        cwd=app_dir,
        timeout_seconds=timeout_seconds,
        # No colour codes in stored evidence, and no update chatter on stderr
        # that would read as a failure to someone looking at the report.
        env={"NO_COLOR": "1", "NPM_CONFIG_UPDATE_NOTIFIER": "false"},
    )


#: vitest's summary line for tests that actually **executed**, e.g.
#: `      Tests  1 passed (1)`.
#:
#: `skipped` is deliberately excluded, and that is the whole subtlety. A `-t`
#: filter matching nothing does not report an empty run: vitest finds the file,
#: marks every test in it skipped, and prints `Tests  2 skipped (2)` with exit
#: code 0. A skipped test did not run and is not evidence of anything.
#:
#: Two earlier versions of this pattern were wrong, both caught by
#: `test_a_filter_that_matches_no_test_yields_no_evidence` asserting against
#: real vitest output rather than an assumed shape. The first used a negative
#: lookahead, `Tests\s+(?!no tests)`, which matched anyway because `\s+` gives
#: back a space under backtracking so the lookahead saw `" no tests"`. The
#: second accepted `skipped`, which is exactly the case being detected.
_VITEST_TESTS_RAN = re.compile(r"^\s*Tests\s+\d+\s+(?:passed|failed)", re.MULTILINE)


def vitest_ran_a_test(result: CommandResult) -> bool:
    """Whether a name-filtered vitest run actually executed a test.

    `passWithNoTests: false` makes vitest fail when no test *file* matches, but
    a `-t` name filter that matches nothing inside a file that does exist exits
    **0**. Measured, not assumed: `test_a_filter_that_matches_no_test_yields_no_evidence`
    runs a deliberately unmatchable filter and observes exit code 0.

    Several checks select one generated test by name, so without this a renamed
    or deleted test would score full marks for a check that ran nothing — the
    exact "green over nothing" failure this phase's review record keeps
    returning to.

    This reads output to establish that the command *did work*, never to decide
    whether the work succeeded. The verdict remains `CommandResult.exit_code`.
    """
    return bool(_VITEST_TESTS_RAN.search(result.stdout))


def _script_command(script: str, *, app_dir: str, timeout_seconds: int) -> Command:
    return Command(
        argv=("npm", "run", script),
        cwd=app_dir,
        timeout_seconds=timeout_seconds,
        env={
            "NO_COLOR": "1",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            # A build that phones home has no network here; disabling telemetry
            # keeps the failure out of the evidence rather than out of the run.
            "NEXT_TELEMETRY_DISABLED": "1",
        },
    )


def harness_registration_path(tool: WebMCPTool) -> str:
    return f"{HARNESS_DIR}/{tool.handler_name}.registration.test.mts"


def harness_schema_path(tool: WebMCPTool) -> str:
    return f"{HARNESS_DIR}/{tool.handler_name}.schema.test.mts"


def plan_checks(
    toolset: WebMCPToolset,
    facts: ApplicationFacts,
    *,
    app_dir: str = ".",
    timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS,
) -> list[PlannedCheck]:
    """The whole suite, as data, before anything is executed.

    Pure: it reads the toolset and the observed facts and returns checks. That
    is what makes "which component does this check feed" reviewable in one
    place instead of being spread through a run loop.
    """
    checks: list[PlannedCheck] = []

    def vitest_check(
        check_id: str,
        component: ScoreComponent,
        description: str,
        test_file: str,
        test_name: str | None,
    ) -> None:
        if not facts.vitest_available:
            checks.append(
                PlannedCheck(
                    check_id=check_id,
                    component=component,
                    description=description,
                    skip_reason=facts.missing_vitest_reason,
                )
            )
            return
        checks.append(
            PlannedCheck(
                check_id=check_id,
                component=component,
                description=description,
                command=_vitest_command(
                    app_dir=app_dir,
                    test_file=test_file,
                    test_name=test_name,
                    timeout_seconds=timeout_seconds,
                ),
            )
        )

    for tool in toolset.tools:
        vitest_check(
            f"registration:{tool.name}",
            ScoreComponent.REGISTRATION_AND_DISCOVERY,
            f"{tool.name} registers with the model context and is discoverable",
            harness_registration_path(tool),
            None,
        )
        vitest_check(
            f"schema:{tool.name}",
            ScoreComponent.SCHEMA_VALIDITY,
            f"{tool.name} declares an input schema matching its approved contract",
            harness_schema_path(tool),
            None,
        )

        if tool.approval_required:
            vitest_check(
                f"authorization:{tool.name}",
                ScoreComponent.AUTHORIZATION_SAFETY,
                f"{tool.name} refuses to act without an approval",
                tool_test_path(tool),
                AUTHORIZATION_TEST_NAME,
            )
        else:
            vitest_check(
                f"execution:{tool.name}",
                ScoreComponent.EXECUTION,
                f"{tool.name} returns a result for valid input",
                tool_test_path(tool),
                EXECUTION_TEST_NAME,
            )

        rejection = rejection_test_name(tool)
        if rejection is None:
            # A tool with no inputs has no wrong-type case, so the generator
            # emits no rejection test. Recorded as a skip with a reason rather
            # than silently omitted, so the report shows an absence.
            #
            # This used to return `""`, which passed an `is not None` guard and
            # became `-t ''` — and vitest treats an empty filter as matching
            # *everything*, so the execution test's green result was scored as
            # ERROR_HANDLING. A zero-input tool collected the full weight for a
            # check that was never generated.
            checks.append(
                PlannedCheck(
                    check_id=f"error_handling:{tool.name}",
                    component=ScoreComponent.ERROR_HANDLING,
                    description=f"{tool.name} rejects input of the wrong type",
                    skip_reason=NO_INPUTS_SKIP_REASON,
                )
            )
        else:
            vitest_check(
                f"error_handling:{tool.name}",
                ScoreComponent.ERROR_HANDLING,
                f"{tool.name} rejects input of the wrong type",
                tool_test_path(tool),
                rejection,
            )

    checks.append(
        PlannedCheck(
            check_id="ui_synchronization",
            component=ScoreComponent.UI_SYNCHRONIZATION,
            description="Application state visible in the DOM tracks tool calls",
            skip_reason=UI_SYNCHRONIZATION_SKIP_REASON,
        )
    )

    checks.append(
        _script_check(
            "regression",
            ScoreComponent.REGRESSION,
            "The application's own test suite still passes",
            "test",
            facts,
            app_dir=app_dir,
            timeout_seconds=timeout_seconds,
        )
    )
    checks.append(
        _script_check(
            "typecheck",
            None,
            "The application still typechecks with the integration applied",
            "typecheck",
            facts,
            app_dir=app_dir,
            timeout_seconds=timeout_seconds,
        )
    )
    checks.append(
        _script_check(
            "build",
            None,
            "The application still builds with the integration applied",
            "build",
            facts,
            app_dir=app_dir,
            timeout_seconds=timeout_seconds,
        )
    )
    return checks


def _script_check(
    check_id: str,
    component: ScoreComponent | None,
    description: str,
    script: str,
    facts: ApplicationFacts,
    *,
    app_dir: str,
    timeout_seconds: int,
) -> PlannedCheck:
    if script not in facts.scripts:
        return PlannedCheck(
            check_id=check_id,
            component=component,
            description=description,
            skip_reason=(
                f"The application declares no {script!r} script, so there is nothing "
                "to run. MCPForge does not invent one."
            ),
        )
    return PlannedCheck(
        check_id=check_id,
        component=component,
        description=description,
        command=_script_command(script, app_dir=app_dir, timeout_seconds=timeout_seconds),
    )


# ---------------------------------------------------------------------------
# The harness, written into the workspace only
# ---------------------------------------------------------------------------


def _config_file() -> str:
    return f"""// MCPForge validation harness — written into the ephemeral validation
// workspace only. It is not part of the generated patch and never reaches your
// repository.
import {{ fileURLToPath }} from "node:url";
import {{ defineConfig }} from "vitest/config";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({{
  root,
  resolve: {{
    alias: [{{ find: /^@\\//, replacement: root + "src/" }}],
  }},
  test: {{
    environment: "node",
    include: ["{HARNESS_DIR}/**/*.test.mts", "src/webmcp/tools/**/*.test.ts"],
    watch: false,
    // Covers the case where no test *file* matched. It does NOT cover a `-t`
    // name filter matching nothing inside a file that does exist: vitest marks
    // every test in that file skipped and exits 0. The validator catches that
    // itself — see `vitest_ran_a_test` in agents/validator.py.
    passWithNoTests: false,
  }},
}});
"""


#: Emitted into every harness file rather than imported from a shared module,
#: so there is no cross-file resolution to get wrong. It is still one source:
#: this function.
#:
#: Public because F8-05's benchmark scenario registers the same generated
#: toolset against the same stub model context and must see exactly what this
#: sees. Two implementations of "register the tools and capture what arrives"
#: is how one of them stops matching the adapter the generator emits.
def capture_helper() -> str:
    return """interface CapturedTool {
  name: string;
  title?: string;
  description: string;
  inputSchema?: Record<string, unknown>;
  execute: (input: Record<string, unknown>) => Promise<unknown>;
}

async function registerAndCapture(): Promise<Record<string, CapturedTool | undefined>> {
  const captured: Record<string, CapturedTool | undefined> = {};
  Object.defineProperty(globalThis, "navigator", {
    value: {
      modelContext: {
        async registerTool(tool: CapturedTool): Promise<void> {
          captured[tool.name] = tool;
        },
      },
    },
    configurable: true,
    writable: true,
  });

  const teardown = registerWebMCPTools();
  // registerWebMCPTools does not await its registrations — it returns a
  // teardown immediately — so let the microtask queue drain first.
  await new Promise((resolve) => setTimeout(resolve, 0));
  teardown();
  return captured;
}
"""


_HARNESS_HEADER = """// MCPForge validation harness — generated for one validation run.
//
// It lives in the ephemeral validation workspace, never in your repository and
// never in the generated patch. It registers the generated tools against a stub
// model context so registration and schema validity can be observed rather than
// assumed.
"""


def alias_specifier(repository_path: str) -> str:
    """Turn a generated file's repository path into its `@/` import specifier.

    Derived from `generation/nextjs.py`'s own path constants rather than
    written out again here, so the harness cannot keep importing a module the
    generator has moved.
    """
    if not repository_path.startswith("src/") or not repository_path.endswith(".ts"):
        raise ValidatorError(
            f"{repository_path!r} is not a generated source path under src/; the harness "
            "import cannot be derived from it"
        )
    return "@/" + repository_path[len("src/") : -len(".ts")]


def _register_import() -> str:
    return f"import {{ registerWebMCPTools }} from {as_ts_string(alias_specifier(REGISTER_PATH))};"


def registration_harness(tool: WebMCPTool) -> str:
    name = as_ts_string(tool.name)
    return f"""{_HARNESS_HEADER}
import {{ describe, expect, it }} from "vitest";

{_register_import()}

{capture_helper()}
describe("mcpforge registration", () => {{
  it("registers and discovers the tool", async () => {{
    const tools = await registerAndCapture();
    const tool = tools[{name}];

    expect(tool, "the tool did not register with the model context").toBeDefined();
    expect(typeof tool!.execute).toBe("function");
    expect(tool!.description.length).toBeGreaterThan(0);
  }});
}});
"""


def schema_harness(tool: WebMCPTool) -> str:
    name = as_ts_string(tool.name)
    expected_properties = as_json_literal(
        sorted(prop.name for prop in tool.inputs), indent=2, reindent="    "
    )
    expected_required = as_json_literal(
        sorted(prop.name for prop in tool.inputs if prop.required), indent=2, reindent="    "
    )
    expected_types = as_json_literal(
        {prop.name: prop.json_type for prop in tool.inputs}, indent=2, reindent="    "
    )
    return f"""{_HARNESS_HEADER}
import {{ describe, expect, it }} from "vitest";

{_register_import()}

{capture_helper()}
describe("mcpforge schema validity", () => {{
  it("declares an input schema matching the approved contract", async () => {{
    const tools = await registerAndCapture();
    const tool = tools[{name}];
    expect(tool, "the tool did not register with the model context").toBeDefined();

    const schema = tool!.inputSchema as
      | {{
          type?: unknown;
          properties?: Record<string, {{ type?: unknown }}>;
          required?: unknown;
          additionalProperties?: unknown;
        }}
      | undefined;

    expect(schema, "the tool registered without an input schema").toBeDefined();
    expect(schema!.type).toBe("object");
    // A schema an agent can rely on is closed: an unknown property must be
    // refused, not ignored.
    expect(schema!.additionalProperties).toBe(false);

    const properties = schema!.properties ?? {{}};
    expect(Object.keys(properties).sort()).toEqual({expected_properties});
    expect(([...((schema!.required as string[]) ?? [])]).sort()).toEqual({expected_required});

    const declared: Record<string, string> = {expected_types};
    for (const [property, jsonType] of Object.entries(declared)) {{
      expect(properties[property]?.type, `${{property}} has the wrong type`).toBe(jsonType);
    }}
  }});
}});
"""


def harness_files(toolset: WebMCPToolset) -> dict[str, str]:
    """Every harness file, keyed by its path relative to the application root."""
    files: dict[str, str] = {HARNESS_CONFIG: _config_file()}
    for tool in toolset.tools:
        files[harness_registration_path(tool)] = registration_harness(tool)
        files[harness_schema_path(tool)] = schema_harness(tool)
    return files


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class ValidationReport(BaseModel):
    """Agent 5's output: what ran, what it did, and the score that follows.

    `passed` is a property, not a stored field, so a persisted report cannot
    carry a verdict that disagrees with the exit codes beside it.
    """

    model_config = ConfigDict(frozen=True)

    checks: tuple[ExecutedCheck, ...]
    skipped: tuple[SkippedCheck, ...]
    score: ReadinessScore

    @property
    def passed(self) -> bool:
        """Every check that ran, passed — and at least one ran.

        A skipped check is not a failure; it is missing evidence, and it has
        already cost its component every one of its points. Keeping the two
        ideas apart is what lets a run be honest about coverage without being
        forced to call an unmeasured component a failure.
        """
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def complete(self) -> bool:
        """Nothing was skipped: every planned check produced evidence."""
        return not self.skipped

    @property
    def failed_check_ids(self) -> tuple[str, ...]:
        return tuple(check.check_id for check in self.checks if not check.passed)


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class Validator:
    """Agent 5. Deterministic, and holds no model provider.

    Constructed with a `SecureExecutionProvider` and nothing else. There is no
    parameter through which a Gemini provider could be supplied, which is the
    first half of "Gemini is never asked for a score"; the second half is the
    AST sweep in `test_validator.py`.
    """

    name = "validator"
    step = "Validating the WebMCP integration"

    def __init__(
        self,
        executor: SecureExecutionProvider,
        *,
        timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS,
    ) -> None:
        self._executor = executor
        self._timeout_seconds = timeout_seconds

    # -- observation ------------------------------------------------------

    def observe(self, workspace: Workspace, *, app_dir: str = ".") -> ApplicationFacts:
        """Read what the workspace actually has. No assumption, no default."""
        root = resolve_inside(workspace, app_dir)
        scripts: set[str] = set()
        manifest = root / "package.json"
        if manifest.is_file():
            try:
                parsed = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                parsed = {}
            declared = parsed.get("scripts") if isinstance(parsed, dict) else None
            if isinstance(declared, dict):
                scripts = {str(k) for k in declared}

        return ApplicationFacts(
            scripts=frozenset(scripts),
            vitest_available=(root / VITEST_CLI).is_file(),
        )

    # -- harness ----------------------------------------------------------

    def write_harness(
        self, workspace: Workspace, toolset: WebMCPToolset, *, app_dir: str = "."
    ) -> list[str]:
        """Write the harness into the workspace. Returns the paths written.

        Every write goes through `resolve_inside`, the single implementation of
        the workspace path jail, so a harness path can never land outside it.
        """
        written: list[str] = []
        for relative, contents in harness_files(toolset).items():
            target: Path = resolve_inside(workspace, f"{app_dir}/{relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
            written.append(relative)
        return sorted(written)

    # -- the run ----------------------------------------------------------

    async def validate(
        self,
        workspace: Workspace,
        toolset: WebMCPToolset,
        *,
        app_dir: str = ".",
    ) -> ValidationReport:
        """Run the suite and compute the score. The whole of agent 5.

        The verdict of every check is `CommandResult.exit_code`. No model is
        consulted, and no output is parsed to decide success or failure.

        One narrow exception, and it only ever makes a check stricter: a
        name-filtered vitest run that exits 0 without executing a test is
        recorded as skipped rather than passed, because vitest exits 0 when a
        `-t` filter matches nothing. See `vitest_ran_a_test`. It cannot turn a
        failure into a pass — a non-zero exit is still a failure — and a check
        that ran nothing yields no evidence, so its component scores zero
        rather than a default.
        """
        if workspace.allow_network:
            raise ValidatorError(
                "validation must run in a workspace with no outbound network "
                "(05_FEATURE_TICKETS.md F8-04, 03_SECURITY_ACCESS.md §3)"
            )

        self.write_harness(workspace, toolset, app_dir=app_dir)
        facts = self.observe(workspace, app_dir=app_dir)
        planned = plan_checks(
            toolset,
            facts,
            app_dir=app_dir,
            timeout_seconds=self._timeout_seconds,
        )

        executed: list[ExecutedCheck] = []
        skipped: list[SkippedCheck] = []

        for check in planned:
            if check.command is None:
                assert check.skip_reason is not None
                skipped.append(
                    SkippedCheck(
                        check_id=check.check_id,
                        component=check.component,
                        description=check.description,
                        reason=check.skip_reason,
                    )
                )
                continue

            try:
                result: CommandResult = await self._executor.run(workspace, check.command)
            except SandboxError as exc:
                # The command never ran, so there is no exit code and therefore
                # no evidence. Recorded as skipped with the sandbox's own
                # reason, rather than scored as a failure we did not observe.
                skipped.append(
                    SkippedCheck(
                        check_id=check.check_id,
                        component=check.component,
                        description=check.description,
                        reason=f"The check could not be executed: {exc}",
                    )
                )
                continue

            if check.selects_one_test and result.ok and not vitest_ran_a_test(result):
                # Exit 0 with nothing executed. Not evidence of anything, so it
                # is an absence rather than a pass: the component scores zero.
                skipped.append(
                    SkippedCheck(
                        check_id=check.check_id,
                        component=check.component,
                        description=check.description,
                        reason=(
                            "vitest exited 0 without running a test: the name filter "
                            "matched nothing, so this check has no evidence"
                        ),
                    )
                )
                continue

            executed.append(
                ExecutedCheck(
                    check_id=check.check_id,
                    component=check.component,
                    description=check.description,
                    evidence=CheckEvidence.from_result(check.check_id, result),
                )
            )
            log.info(
                "validator.check",
                check_id=check.check_id,
                component=check.component.value if check.component else None,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
            )

        report = ValidationReport(
            checks=tuple(executed),
            skipped=tuple(skipped),
            score=score_components(executed),
        )
        log.info(
            "validator.completed",
            checks_executed=len(executed),
            checks_skipped=len(skipped),
            passed=report.passed,
            score=report.score.total,
        )
        return report
