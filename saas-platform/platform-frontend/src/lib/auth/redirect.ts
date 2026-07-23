const DEFAULT_REDIRECT = '/dashboard'

function isAllowedPlatformHost(hostname: string, platformDomain: string): boolean {
  const domain = platformDomain.trim().toLowerCase()
  const host = hostname.toLowerCase()
  return Boolean(domain && (host === domain || host.endsWith(`.${domain}`)))
}

function isProtocolRelativeRedirect(target: string): boolean {
  return target.replaceAll('\\', '/').startsWith('//')
}

/** Restrict post-auth redirects to local paths or HTTPS URLs on the platform domain. */
export function sanitizePostAuthRedirect(
  target: string | null | undefined,
  platformDomain = ''
): string {
  if (!target || isProtocolRelativeRedirect(target)) {
    return DEFAULT_REDIRECT
  }

  if (target.startsWith('/')) {
    return target
  }

  try {
    const url = new URL(target)
    if (
      url.protocol === 'https:' &&
      !url.username &&
      !url.password &&
      isAllowedPlatformHost(url.hostname, platformDomain)
    ) {
      return url.toString()
    }
  } catch {
    return DEFAULT_REDIRECT
  }

  return DEFAULT_REDIRECT
}
