const DEFAULT_REDIRECT = '/dashboard'

function isAllowedPlatformHost(hostname: string, platformDomain: string): boolean {
  const domain = platformDomain.trim().toLowerCase()
  const host = hostname.toLowerCase()
  return Boolean(domain && (host === domain || host.endsWith(`.${domain}`)))
}

function leavesPlatformOrigin(target: string): boolean {
  // The WHATWG URL parser removes ASCII tab and newline before parsing, so "/\t/evil.example"
  // resolves to the scheme-relative "//evil.example". Reject every C0 control character instead
  // of replaying that removal, since no redirect target needs one.
  if ([...target].some((character) => character < ' ')) {
    return true
  }
  return target.replaceAll('\\', '/').startsWith('//')
}

/** Restrict post-auth redirects to local paths or HTTPS URLs on the platform domain. */
export function sanitizePostAuthRedirect(
  target: string | null | undefined,
  platformDomain = ''
): string {
  if (!target || leavesPlatformOrigin(target)) {
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
