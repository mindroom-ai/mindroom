import type { NextConfig } from "next";
const nextConfig: NextConfig = {
  /* config options here */
  typescript: {
    // !! WARN !!
    // Dangerously allow production builds to successfully complete even if
    // your project has type errors.
    // !! WARN !!
    ignoreBuildErrors: true,
  },
  eslint: {
    // Warning: This allows production builds to successfully complete even if
    // your project has ESLint errors.
    ignoreDuringBuilds: true,
  },
  output: 'standalone',
  // Silence Turbopack workspace root warning
  // (Next.js will use this directory as the workspace root)
  // @ts-expect-error - 'turbopack' is not yet in typed NextConfig
  turbopack: {
    root: __dirname,
  },
};

export default nextConfig;
