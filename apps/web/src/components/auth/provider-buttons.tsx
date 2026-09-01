"use client";

import { Button } from "@/components/ui/button";
import { ALL_PROVIDERS, type ProviderId } from "@/lib/env";

const LABELS: Record<ProviderId, string> = {
  google: "Continue with Google",
  github: "Continue with GitHub",
  microsoft: "Continue with Microsoft",
  apple: "Continue with Apple",
  password: "Continue with email",
};

/**
 * Sign-in options — 04_FRONTEND_SPEC.md, 01_PRD.md §11.
 *
 * A provider the deployment has not configured renders disabled with the reason
 * stated. Providers are never faked, and a disabled one is not clickable.
 */
export function ProviderButtons({
  enabled,
  onSelect,
}: {
  enabled: readonly ProviderId[];
  onSelect: (provider: ProviderId) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      {ALL_PROVIDERS.map((provider) => {
        const isEnabled = enabled.includes(provider);
        return (
          <Button
            key={provider}
            variant={provider === "google" && isEnabled ? "primary" : "secondary"}
            disabled={!isEnabled}
            disabledReason="Not configured for this deployment"
            onClick={isEnabled ? () => onSelect(provider) : undefined}
          >
            {LABELS[provider]}
            {!isEnabled ? (
              <span className="text-xs font-normal opacity-80">· not configured</span>
            ) : null}
          </Button>
        );
      })}
    </div>
  );
}
