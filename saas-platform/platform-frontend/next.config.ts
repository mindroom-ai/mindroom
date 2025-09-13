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
  async headers() {
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'https://api.staging.mindroom.chat'
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || ''

    const connectSrc = ["'self'", 'https:', 'wss:']
    if (supabaseUrl) connectSrc.push(supabaseUrl)
    if (apiUrl) connectSrc.push(apiUrl)

    const csp = [
      "default-src 'self'",
      "base-uri 'self'",
      "frame-ancestors 'none'",
      "object-src 'none'",
      "img-src 'self' data: blob:",
      "font-src 'self' data:",
      "script-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      `connect-src ${connectSrc.join(' ')}`,
      'upgrade-insecure-requests',
    ].join('; ')

    return [
      {
        source: '/(.*)',
        headers: [
          { key: 'Content-Security-Policy-Report-Only', value: csp },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
          { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=()' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'DENY' },
        ],
      },
    ]
  },
};

export default nextConfig;
