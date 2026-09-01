import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readPublicEnv } from "@/lib/env";

const KEYS = [
  "NEXT_PUBLIC_FIREBASE_API_KEY",
  "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN",
  "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
  "NEXT_PUBLIC_FIREBASE_APP_ID",
  "NEXT_PUBLIC_AUTH_PROVIDERS",
  "NEXT_PUBLIC_WEBMCP_MOCK",
  "NEXT_PUBLIC_API_BASE_URL",
] as const;

const original: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of KEYS) {
    original[k] = process.env[k];
    delete process.env[k];
  }
});

afterEach(() => {
  for (const k of KEYS) {
    if (original[k] === undefined) delete process.env[k];
    else process.env[k] = original[k];
  }
});

function configureFirebase() {
  process.env.NEXT_PUBLIC_FIREBASE_API_KEY = "public-web-key";
  process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN = "x.firebaseapp.com";
  process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID = "x";
  process.env.NEXT_PUBLIC_FIREBASE_APP_ID = "1:2:web:3";
}

describe("public environment", () => {
  it("reports firebase as unconfigured when the config is incomplete", () => {
    process.env.NEXT_PUBLIC_FIREBASE_API_KEY = "only-one-value";
    expect(readPublicEnv().firebase).toBeNull();
  });

  it("reads a complete firebase config", () => {
    configureFirebase();
    expect(readPublicEnv().firebase).toEqual({
      apiKey: "public-web-key",
      authDomain: "x.firebaseapp.com",
      projectId: "x",
      appId: "1:2:web:3",
    });
  });

  it("refuses to enable a provider without the config needed to run it", () => {
    process.env.NEXT_PUBLIC_AUTH_PROVIDERS = "google";
    expect(readPublicEnv().enabledProviders).toEqual([]);
  });

  it("enables configured providers when firebase is present", () => {
    configureFirebase();
    process.env.NEXT_PUBLIC_AUTH_PROVIDERS = "google, github";
    expect(readPublicEnv().enabledProviders).toEqual(["google", "github"]);
  });

  it("throws on an unknown provider rather than silently ignoring it", () => {
    configureFirebase();
    process.env.NEXT_PUBLIC_AUTH_PROVIDERS = "google,myspace";
    expect(() => readPublicEnv()).toThrow(/unknown provider/i);
  });

  it("defaults to no providers when none are listed", () => {
    configureFirebase();
    expect(readPublicEnv().enabledProviders).toEqual([]);
  });

  it("never enables the mock WebMCP adapter in a production build", () => {
    process.env.NEXT_PUBLIC_WEBMCP_MOCK = "true";
    vi.stubEnv("NODE_ENV", "production");
    try {
      expect(readPublicEnv().webmcpMock).toBe(false);
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("allows the mock adapter outside production when explicitly opted in", () => {
    process.env.NEXT_PUBLIC_WEBMCP_MOCK = "true";
    expect(readPublicEnv().webmcpMock).toBe(true);
  });

  it("does not enable the mock adapter by default", () => {
    expect(readPublicEnv().webmcpMock).toBe(false);
  });
});
