import type { NextConfig } from "next";

// Next.js and Supabase Auth UI emit inline bootstrap assets in this build.
// Move script/style directives to nonces once per-request CSP middleware exists.
const securityHeaders = [
  {
    key: 'Content-Security-Policy',
    value: [
      "default-src 'self'",
      "base-uri 'self'",
      "frame-ancestors 'none'",
      "object-src 'none'",
      "img-src 'self' data: blob: https:",
      "font-src 'self' data:",
      "script-src 'self' 'unsafe-inline'",
      "style-src 'self' 'unsafe-inline'",
      "connect-src 'self' https: wss:",
      "frame-src 'self' https://js.stripe.com https://hooks.stripe.com",
      "form-action 'self'",
      "media-src 'self'",
      "worker-src 'self' blob:",
      'upgrade-insecure-requests',
      'report-uri /api/csp-report',
    ].join('; '),
  },
  {
    key: 'Referrer-Policy',
    value: 'strict-origin-when-cross-origin',
  },
  {
    key: 'Permissions-Policy',
    value: 'camera=(), microphone=(), geolocation=(), payment=(self)',
  },
  {
    key: 'X-Content-Type-Options',
    value: 'nosniff',
  },
  {
    key: 'X-Frame-Options',
    value: 'DENY',
  },
  {
    key: 'Strict-Transport-Security',
    value: 'max-age=31536000; includeSubDomains; preload',
  },
]

const nextConfig: NextConfig = {
  poweredByHeader: false,
  output: 'standalone',
  // Silence Turbopack workspace root warning in this monorepo.
  turbopack: {
    root: __dirname,
  },
  async headers() {
    return [
      {
        source: '/(.*)',
        headers: securityHeaders,
      },
    ]
  },
};

export default nextConfig;
