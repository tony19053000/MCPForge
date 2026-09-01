/**
 * Client environment validation — 05_FEATURE_TICKETS.md F1-03.
 *
 * Only NEXT_PUBLIC_* values exist here, and only genuinely public ones. A
 * missing optional integration reports itself unconfigured; nothing is faked
 * and no default weakens a control.
 */

export type ProviderId = "google" | "github" | "microsoft" | "apple" | "password";

export const ALL_PROVIDERS: readonly ProviderId[] = [
  "google",
  "github",
  "microsoft",
  "apple",
  "password",
] as const;

export interface FirebaseWebConfig {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
}

export interface PublicEnv {
  apiBaseUrl: string;
  firebase: FirebaseWebConfig | null;
  /** Providers the deployment has actually configured. Others render disabled. */
  enabledProviders: readonly ProviderId[];
  /** Dev-only labelled mock WebMCP adapter. Never true in a production build. */
  webmcpMock: boolean;
}

function readFirebase(): FirebaseWebConfig | null {
  const apiKey = process.env.NEXT_PUBLIC_FIREBASE_API_KEY;
  const authDomain = process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN;
  const projectId = process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID;
  const appId = process.env.NEXT_PUBLIC_FIREBASE_APP_ID;

  if (!apiKey || !authDomain || !projectId || !appId) return null;
  return { apiKey, authDomain, projectId, appId };
}

function readProviders(): readonly ProviderId[] {
  const raw = process.env.NEXT_PUBLIC_AUTH_PROVIDERS ?? "";
  const requested = raw
    .split(",")
    .map((p) => p.trim().toLowerCase())
    .filter(Boolean);

  const unknown = requested.filter((p) => !ALL_PROVIDERS.includes(p as ProviderId));
  if (unknown.length > 0) {
    throw new Error(
      `NEXT_PUBLIC_AUTH_PROVIDERS lists unknown provider(s): ${unknown.join(", ")}. ` +
        `Known providers: ${ALL_PROVIDERS.join(", ")}.`,
    );
  }
  return requested as ProviderId[];
}

export function readPublicEnv(): PublicEnv {
  const firebase = readFirebase();
  let enabledProviders = readProviders();

  // A provider cannot be enabled without the config needed to run it. Claiming
  // otherwise would render a sign-in button that cannot work.
  if (!firebase && enabledProviders.length > 0) {
    enabledProviders = [];
  }

  const mockRequested = process.env.NEXT_PUBLIC_WEBMCP_MOCK === "true";
  const isProduction = process.env.NODE_ENV === "production";

  return {
    apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
    firebase,
    enabledProviders,
    webmcpMock: mockRequested && !isProduction,
  };
}

export const env: PublicEnv = readPublicEnv();
