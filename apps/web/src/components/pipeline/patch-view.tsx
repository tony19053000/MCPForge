import { DiffView } from "@/components/diff/diff-view";
import type { PatchDto } from "@/lib/api/types";

/**
 * The generated patch as stored — ticket T5b. The PATCH and PULL_REQUEST
 * approvals bind to `artifact_hash`, so the version shown here is the one an
 * approval card beside it is checked against.
 */
export function PatchView({ patch }: { patch: PatchDto }) {
  return (
    <section aria-labelledby="patch-heading" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="patch-heading" className="text-sm font-medium text-text">
          Generated patch
        </h3>
        <span className="font-mono text-xs text-subtle">
          version {patch.artifact_hash.slice(0, 12)}
        </span>
      </div>
      <p className="text-sm text-muted">{patch.summary}</p>
      <p className="text-xs text-subtle">
        Base commit{" "}
        <span className="font-mono">{patch.base_commit ? patch.base_commit.slice(0, 12) : "not recorded"}</span>
      </p>
      <DiffView files={patch.files} />
    </section>
  );
}
