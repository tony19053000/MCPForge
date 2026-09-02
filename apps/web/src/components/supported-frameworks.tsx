import { env } from "@/lib/env";

/**
 * What MCPForge actually supports, read from the backend's adapter registry.
 *
 * 01_PRD.md §9: framework support is a table in the product, not a marketing
 * claim. The page once said "Next.js, React and TypeScript applications", which
 * was broader than the one registered adapter. This asks rather than asserts,
 * and says so plainly when it cannot reach the API.
 */

interface Framework {
  framework: string;
  display_name: string;
}

export async function SupportedFrameworks() {
  let frameworks: Framework[] | null = null;

  try {
    const response = await fetch(`${env.apiBaseUrl}/api/frameworks`, {
      // Support changes when an adapter is added, not on a timer.
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    if (response.ok) frameworks = (await response.json()) as Framework[];
  } catch {
    // Unreachable API. Say nothing rather than claim something.
  }

  if (!frameworks || frameworks.length === 0) {
    return <span>Supported frameworks unavailable</span>;
  }

  return <span>Supports {frameworks.map((f) => f.display_name).join(", ")}</span>;
}
