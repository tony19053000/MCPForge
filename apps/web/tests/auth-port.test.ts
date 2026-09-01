/**
 * The web auth port must be vendor-agnostic — 02_ARCHITECTURE.md §3.2.
 *
 * Firebase Auth is provisional. These tests hold the boundary that makes
 * replacing it a one-adapter change.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { UnconfiguredAuthProvider } from "@/lib/auth/firebase-provider";
import { AuthNotConfiguredError, type AuthProvider, type Session } from "@/lib/auth/types";
import type { ProviderId } from "@/lib/env";

const SRC = path.resolve(__dirname, "../src");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

describe("auth boundary", () => {
  it("confines the firebase import to the adapter and its factory", () => {
    const offenders = walk(SRC)
      .filter((f) => /\.tsx?$/.test(f))
      .filter((f) => /^\s*import .*["']firebase\//m.test(readFileSync(f, "utf8")))
      .map((f) => path.relative(SRC, f));

    expect(offenders).toEqual(["lib/auth/firebase-provider.ts"]);
  });

  it("accepts an unrelated implementation of the port", async () => {
    class StaticAuthProvider implements AuthProvider {
      readonly availableProviders = ["google"] as const;
      async signIn(provider: ProviderId): Promise<Session> {
        if (!this.availableProviders.includes(provider as "google")) {
          throw new Error(`unsupported: ${provider}`);
        }
        return {
          subject: "sub-1",
          email: "a@b.test",
          displayName: null,
          photoUrl: null,
          getIdToken: async () => "token",
        };
      }
      async signOut(): Promise<void> {}
      currentSession(): Session | null {
        return null;
      }
      onChange(): () => void {
        return () => {};
      }
    }

    const provider: AuthProvider = new StaticAuthProvider();
    expect((await provider.signIn("google")).subject).toBe("sub-1");
  });

  it("refuses honestly when unconfigured instead of simulating a session", async () => {
    const provider = new UnconfiguredAuthProvider();
    expect(provider.currentSession()).toBeNull();
    await expect(provider.signIn()).rejects.toBeInstanceOf(AuthNotConfiguredError);
  });

  it("has no development bypass that fabricates a signed-in user", () => {
    const suspicious = walk(SRC)
      .filter((f) => /\.tsx?$/.test(f))
      .filter((f) => /fakeUser|mockSession|bypassAuth|DEV_USER/i.test(readFileSync(f, "utf8")))
      .map((f) => path.relative(SRC, f));
    expect(suspicious).toEqual([]);
  });
});

describe("the web tier cannot reach a model", () => {
  it("imports no Gemini SDK and holds no model configuration", () => {
    // 02_ARCHITECTURE.md §1: all AI calls live in the backend. If a model
    // client ever appeared in the browser bundle, the API key would follow.
    const offenders = walk(SRC)
      .filter((f) => /\.tsx?$/.test(f))
      .filter((f) => /@google\/genai|generativelanguage|GEMINI_API_KEY/i.test(readFileSync(f, "utf8")))
      .map((f) => path.relative(SRC, f));
    expect(offenders).toEqual([]);
  });
});
