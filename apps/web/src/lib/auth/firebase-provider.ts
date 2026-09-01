/**
 * Firebase adapter for the AuthProvider port — PROVISIONAL.
 *
 * Firebase Auth is the current implementation, not the decided architecture
 * (02_ARCHITECTURE.md §3.2). Everything Firebase-specific is confined to this
 * file; consumers see only `Session` and `AuthProvider`.
 */

import { type FirebaseApp, getApps, initializeApp } from "firebase/app";
import {
  GithubAuthProvider,
  GoogleAuthProvider,
  OAuthProvider,
  type Auth,
  type User,
  getAuth,
  onAuthStateChanged,
  signInWithPopup,
  signOut as firebaseSignOut,
} from "firebase/auth";

import type { FirebaseWebConfig, ProviderId } from "@/lib/env";
import {
  AuthNotConfiguredError,
  ProviderNotEnabledError,
  type AuthProvider,
  type Session,
  type Unsubscribe,
} from "@/lib/auth/types";

function toSession(user: User): Session {
  return {
    subject: user.uid,
    email: user.email,
    displayName: user.displayName,
    photoUrl: user.photoURL,
    getIdToken: () => user.getIdToken(),
  };
}

function popupProviderFor(provider: ProviderId) {
  switch (provider) {
    case "google":
      return new GoogleAuthProvider();
    case "github":
      return new GithubAuthProvider();
    case "microsoft":
      return new OAuthProvider("microsoft.com");
    case "apple":
      return new OAuthProvider("apple.com");
    case "password":
      // Email/password is not a popup flow and is not implemented. Rather than
      // pretend, we refuse — and the UI keeps the option disabled.
      throw new ProviderNotEnabledError(provider);
  }
}

export class FirebaseAuthProvider implements AuthProvider {
  private readonly app: FirebaseApp;
  private readonly auth: Auth;

  constructor(
    config: FirebaseWebConfig,
    readonly availableProviders: readonly ProviderId[],
  ) {
    this.app = getApps()[0] ?? initializeApp(config);
    this.auth = getAuth(this.app);
  }

  async signIn(provider: ProviderId): Promise<Session> {
    if (!this.availableProviders.includes(provider)) {
      throw new ProviderNotEnabledError(provider);
    }
    const result = await signInWithPopup(this.auth, popupProviderFor(provider));
    return toSession(result.user);
  }

  async signOut(): Promise<void> {
    await firebaseSignOut(this.auth);
  }

  currentSession(): Session | null {
    const user = this.auth.currentUser;
    return user ? toSession(user) : null;
  }

  onChange(callback: (session: Session | null) => void): Unsubscribe {
    return onAuthStateChanged(this.auth, (user) => callback(user ? toSession(user) : null));
  }
}

/**
 * Stands in when the deployment has no identity configuration.
 *
 * It refuses honestly rather than simulating a signed-in user. There is no
 * "dev bypass" session anywhere in this codebase.
 */
export class UnconfiguredAuthProvider implements AuthProvider {
  readonly availableProviders: readonly ProviderId[] = [];

  async signIn(): Promise<Session> {
    throw new AuthNotConfiguredError();
  }
  async signOut(): Promise<void> {}
  currentSession(): Session | null {
    return null;
  }
  onChange(callback: (session: Session | null) => void): Unsubscribe {
    callback(null);
    return () => {};
  }
}
