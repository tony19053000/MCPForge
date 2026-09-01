import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

describe("Button", () => {
  it("is keyboard operable", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Approve</Button>);
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Approve" })).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("states why it is unavailable instead of failing silently", () => {
    render(
      <Button disabled disabledReason="GitHub is not connected">
        Create pull request
      </Button>,
    );
    const button = screen.getByRole("button");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAttribute("title", "GitHub is not connected");
  });

  it("does not fire when disabled", async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Nope
      </Button>,
    );
    await userEvent.click(screen.getByRole("button"));
    expect(onClick).not.toHaveBeenCalled();
  });

  it("defaults to type=button so it never submits a form by accident", () => {
    render(<Button>Safe</Button>);
    expect(screen.getByRole("button")).toHaveAttribute("type", "button");
  });
});

describe("Badge", () => {
  it("always carries a text label, never colour alone", () => {
    render(<Badge tone="danger">DESTRUCTIVE</Badge>);
    expect(screen.getByText("DESTRUCTIVE")).toBeInTheDocument();
  });

  it("hides a decorative glyph from assistive technology", () => {
    render(
      <Badge tone="warning" glyph="!">
        WRITE
      </Badge>,
    );
    expect(screen.getByText("!")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("WRITE")).toBeInTheDocument();
  });
});

describe("Card", () => {
  it("renders its children and forwards attributes", () => {
    render(<Card data-testid="card">contents</Card>);
    expect(screen.getByTestId("card")).toHaveTextContent("contents");
  });
});

describe("Skeleton", () => {
  it("announces itself as a loading state", () => {
    render(<Skeleton className="h-4 w-32" />);
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
  });
});
