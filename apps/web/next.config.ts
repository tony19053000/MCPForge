import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  // MCPForge never ships a build that typechecks dirty. Lint runs as its own
  // gate in CI (Next 16 no longer accepts an `eslint` key here).
  typescript: { ignoreBuildErrors: false },
};

export default nextConfig;
