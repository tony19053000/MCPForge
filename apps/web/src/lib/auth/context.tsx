"use client";

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { createAuthProvider } from "@/lib/auth/create-provider";
import type { AuthProvider, Session } from "@/lib/auth/types";
import { ApiClient } from "@/lib/api/client";
import type { ProviderId } from "@/lib/env";

/**
 * Auth state for the app.
 *
 * Holds only our own `Session` type, so nothing below this line knows which
 * identity provider is in use (02_ARCHITECTURE.md §3.2).
 */

interface AuthState {
  session: Session | null;
  ready: boolean;
  availableProviders: readonly ProviderId[];
  signIn(provider: ProviderId): Promise<void>;
  signOut(): Promise<void>;
  api: ApiClient;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProviderContext({
  children,
  provider,
}: {
  children: ReactNode;
  /** Injectable so tests exercise the real components without Firebase. */
  provider?: AuthProvider;
}) {
  const [auth] = useState<AuthProvider>(() => provider ?? createAuthProvider());
  const [session, setSession] = useState<Session | null>(() => auth.currentSession());
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const unsubscribe = auth.onChange((next) => {
      setSession(next);
      setReady(true);
    });
    return unsubscribe;
  }, [auth]);

  const value = useMemo<AuthState>(() => {
    const api = new ApiClient(async () => {
      const current = auth.currentSession();
      if (!current) throw new Error("You are signed out. Sign in to continue.");
      return current.getIdToken();
    });
    return {
      session,
      ready,
      availableProviders: auth.availableProviders,
      signIn: async (p) => {
        setSession(await auth.signIn(p));
      },
      signOut: async () => {
        await auth.signOut();
        setSession(null);
      },
      api,
    };
  }, [auth, session, ready]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProviderContext");
  return context;
}
