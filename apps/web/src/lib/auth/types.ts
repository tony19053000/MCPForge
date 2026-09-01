/**
 * The AuthProvider port — 02_ARCHITECTURE.md §3.2.
 *
 * `Session` is MCPForge's own type. No Firebase object crosses this boundary,
 * so replacing Firebase Auth with direct Google OAuth means writing one new
 * adapter and changing nothing in the components that consume it.
 */

import type { ProviderId } from "@/lib/env";

export interface Session {
  /** Stable unique user id from the issuer. */
  readonly subject: string;
  readonly email: string | null;
  readonly displayName: string | null;
  readonly photoUrl: string | null;
  /** Bearer token for the API. The backend verifies it; the client never trusts it. */
  getIdToken(): Promise<string>;
}

export type Unsubscribe = () => void;

export interface AuthProvider {
  /** Providers this deployment can actually use. */
  readonly availableProviders: readonly ProviderId[];
  signIn(provider: ProviderId): Promise<Session>;
  signOut(): Promise<void>;
  currentSession(): Session | null;
  onChange(callback: (session: Session | null) => void): Unsubscribe;
}

export class AuthNotConfiguredError extends Error {
  constructor(message = "Authentication is not configured for this deployment") {
    super(message);
    this.name = "AuthNotConfiguredError";
  }
}

export class ProviderNotEnabledError extends Error {
  constructor(public readonly provider: ProviderId) {
    super(`Sign-in provider '${provider}' is not configured for this deployment`);
    this.name = "ProviderNotEnabledError";
  }
}
