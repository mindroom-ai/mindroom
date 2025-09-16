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
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || (process.env.PLATFORM_DOMAIN ? `https://api.${process.env.PLATFORM_DOMAIN}` : 'http://localhost:8000')
    const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || ''
    const isDev = process.env.NODE_ENV !== 'production'

    // Build connect-src whitelist
    const connectSrc = ["'self'"]
    if (supabaseUrl) {
      const supabaseHost = new URL(supabaseUrl).origin
      connectSrc.push(supabaseHost)
      connectSrc.push(supabaseHost.replace('https://', 'wss://'))  // WebSocket
    }
    if (apiUrl) connectSrc.push(new URL(apiUrl).origin)

    // Stripe for payments
    connectSrc.push('https://api.stripe.com')

    // Development only
    if (isDev) {
      connectSrc.push('http://localhost:*')
      connectSrc.push('ws://localhost:*')
    }

    // Build CSP header with proper security
    const cspDirectives = [
      "default-src 'self'",
      "base-uri 'self'",
      "frame-ancestors 'none'",
      "object-src 'none'",
      "img-src 'self' data: blob: https:",
      "font-src 'self' data:",
      // Next.js requires 'unsafe-eval' in dev mode only
      isDev
        ? "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
        : "script-src 'self' 'unsafe-inline'",
      // Style needs unsafe-inline for Next.js styled-jsx
      "style-src 'self' 'unsafe-inline'",
      `connect-src ${connectSrc.join(' ')}`,
      // Stripe frame for payment processing
      "frame-src 'self' https://js.stripe.com https://hooks.stripe.com",
      // Form submissions
      "form-action 'self'",
      // Media
      "media-src 'self'",
      // Workers
      "worker-src 'self' blob:",
      // Upgrade HTTP to HTTPS
      isDev ? '' : 'upgrade-insecure-requests',
      // CSP violation reporting
      'report-uri /api/csp-report',
    ].filter(Boolean).join('; ')

    return [
      {
        source: '/(.*)',
        headers: [
          // Use enforcing CSP in production, report-only in dev
          {
            key: isDev ? 'Content-Security-Policy-Report-Only' : 'Content-Security-Policy',
            value: cspDirectives
          },
          // Security headers
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
          { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=(), payment=(self)' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'X-XSS-Protection', value: '1; mode=block' },
          // HSTS (only in production)
          ...(isDev ? [] : [
            {
              key: 'Strict-Transport-Security',
              value: 'max-age=31536000; includeSubDomains; preload'
            }
          ]),
        ],
      },
    ]
  },
};

export default nextConfig;
