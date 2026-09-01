import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ProviderButtons } from "@/components/auth/provider-buttons";

describe("sign-in providers", () => {
  it("enables only the providers the deployment configured", () => {
    render(<ProviderButtons enabled={["google"]} onSelect={() => {}} />);
    expect(screen.getByRole("button", { name: /Continue with Google/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Continue with GitHub/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Continue with Apple/ })).toBeDisabled();
  });

  it("says why a provider is unavailable rather than hiding it or faking it", () => {
    render(<ProviderButtons enabled={["google"]} onSelect={() => {}} />);
    const github = screen.getByRole("button", { name: /Continue with GitHub/ });
    expect(github).toHaveAttribute("title", "Not configured for this deployment");
    expect(github).toHaveTextContent("not configured");
  });

  it("does not invoke sign-in for a disabled provider", async () => {
    const onSelect = vi.fn();
    render(<ProviderButtons enabled={["google"]} onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: /Continue with Microsoft/ }));
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("invokes sign-in for an enabled provider", async () => {
    const onSelect = vi.fn();
    render(<ProviderButtons enabled={["google"]} onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: /Continue with Google/ }));
    expect(onSelect).toHaveBeenCalledWith("google");
  });

  it("renders nothing enabled when no provider is configured", () => {
    render(<ProviderButtons enabled={[]} onSelect={() => {}} />);
    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
  });
});
