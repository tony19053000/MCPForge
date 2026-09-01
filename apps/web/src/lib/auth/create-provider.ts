import { env } from "@/lib/env";
import type { AuthProvider } from "@/lib/auth/types";
import { FirebaseAuthProvider, UnconfiguredAuthProvider } from "@/lib/auth/firebase-provider";

/**
 * Selects the auth adapter for this deployment.
 *
 * The single place that names a concrete provider. Swapping Firebase Auth for
 * direct Google OAuth is a change here plus one new adapter file.
 */
export function createAuthProvider(): AuthProvider {
  if (!env.firebase || env.enabledProviders.length === 0) {
    return new UnconfiguredAuthProvider();
  }
  return new FirebaseAuthProvider(env.firebase, env.enabledProviders);
}
