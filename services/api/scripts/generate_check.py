"""Generate WebMCP tools for the demo app and typecheck the result.

Writes the patch into a throwaway copy of the fixture and runs `tsc` over it, so
"the generated code compiles" is observed rather than assumed.

    uv run python scripts/generate_check.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mcpforge.generation.nextjs import generate_patch
from mcpforge.models.webmcp import (
    CallStyle,
    SourceBinding,
    ToolInputProperty,
    WebMCPTool,
    WebMCPToolset,
)

DEMO = Path(__file__).resolve().parents[3] / "fixtures" / "demo-hotel-app"

TOOLSET = WebMCPToolset(
    tools=[
        WebMCPTool(
            name="search_rooms",
            title="Search rooms",
            description="Find rooms matching a guest count and price ceiling.",
            inputs=[
                ToolInputProperty(
                    name="guests", json_type="integer", description="Number of guests"
                ),
                ToolInputProperty(
                    name="maxPrice",
                    json_type="number",
                    description="Highest nightly price",
                    required=False,
                ),
            ],
            output_description="Rooms matching the criteria.",
            risk="READ",
            approval_required=False,
            source=SourceBinding(
                module="@/lib/rooms",
                symbol="searchRooms",
                call_style=CallStyle.OBJECT,
                parameters=["guests", "maxPrice"],
            ),
            evidence=[{"path": "src/lib/rooms.ts", "symbol": "searchRooms"}],
        ),
        WebMCPTool(
            name="check_availability",
            title="Check room availability",
            description="Check whether a room is free for a date range.",
            inputs=[
                ToolInputProperty(name="roomId", json_type="string", description="Room id"),
                ToolInputProperty(name="checkIn", json_type="string", description="ISO date"),
                ToolInputProperty(name="checkOut", json_type="string", description="ISO date"),
            ],
            output_description="Availability and total price.",
            risk="READ",
            approval_required=False,
            source=SourceBinding(
                module="@/lib/availability",
                symbol="checkAvailability",
                parameters=["roomId", "checkIn", "checkOut"],
            ),
            evidence=[{"path": "src/lib/availability.ts", "symbol": "checkAvailability"}],
        ),
        WebMCPTool(
            name="cancel_reservation",
            title="Cancel a reservation",
            description="Cancel an existing booking.",
            inputs=[
                ToolInputProperty(
                    name="reservationId", json_type="string", description="Booking id"
                )
            ],
            output_description="The cancelled reservation.",
            risk="DESTRUCTIVE",
            approval_required=True,
            source=SourceBinding(
                module="@/lib/reservations",
                symbol="cancelReservation",
                parameters=["reservationId"],
            ),
            evidence=[{"path": "src/lib/reservations.ts", "symbol": "cancelReservation"}],
        ),
    ]
)


def main() -> int:
    patch = generate_patch(TOOLSET, base_commit="demo")
    print(f"generated : {len(patch.files)} files, +{patch.total_added}/-{patch.total_removed}")
    for change in patch.files:
        print(f"  {change.path}")

    workspace = Path(tempfile.mkdtemp(prefix="mcpforge-generate-"))
    target = workspace / "app"
    shutil.copytree(DEMO, target, ignore=shutil.ignore_patterns("node_modules", ".next"))

    try:
        # The fixture's dependencies live in the workspace root. Without them
        # every JSX line reports an error and the real ones are lost in noise.
        node_modules = DEMO.parents[1] / "node_modules"
        if node_modules.is_dir():
            (target / "node_modules").symlink_to(node_modules)

        for change in patch.files:
            destination = target / change.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(change.contents)

        # The repo's own compiler, not whatever npx would fetch.
        tsc = DEMO.parents[1] / "node_modules" / ".bin" / "tsc"
        if not tsc.is_file():
            print(f"tsc not found at {tsc}; run npm install first")
            return 2

        print("\nrunning tsc over the patched application...")
        result = subprocess.run(  # noqa: S603
            [str(tsc), "--noEmit", "-p", "tsconfig.json"],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode == 0:
            print("tsc       : clean — the generated code compiles inside the real app")
        else:
            print(f"tsc       : FAILED ({result.returncode})")
            print(result.stdout[-3000:])
            print(result.stderr[-1000:])
            return 1

        # The property that matters most: handlers call the developer's code.
        print("\nreuse check:")
        for tool in TOOLSET.tools:
            source = (target / f"src/webmcp/tools/{tool.handler_name}.ts").read_text()
            imported = f"{tool.source.symbol} as {tool.import_alias}" in source
            print(f"  {tool.name:22} imports {tool.source.symbol}: {imported}")
            if not imported:
                return 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    print("\nGenerated integration compiles and reuses existing logic.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
