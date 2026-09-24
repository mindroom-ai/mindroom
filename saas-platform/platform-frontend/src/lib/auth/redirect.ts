export const DEFAULT_POST_AUTH_REDIRECT = '/dashboard'

// Placeholder origin for resolving local paths the way the browser will.
const LOCAL_ORIGIN = 'https://local.invalid'

function normalizeDomain(platformDomain: string): string {
  return platformDomain.trim().toLowerCase()
}

function isPlatformSubdomain(hostname: string, domain: string): boolean {
  return Boolean(domain && (hostname === domain || hostname.endsWith(`.${domain}`)))
}

/** Hosts the platform operates itself; every other subdomain may be a customer's instance. */
function isPlatformOperatedHost(host: string, domain: string): boolean {
  return Boolean(domain && (host === `app.${domain}` || host === `api.${domain}`))
}

/** Normalize a path that must stay on the current origin, even after tab, newline, or backslash handling. */
function localPath(target: string): string | null {
  try {
    const url = new URL(target, LOCAL_ORIGIN)
    const path = `${url.pathname}${url.search}${url.hash}`
    return url.origin === LOCAL_ORIGIN && !path.startsWith('//') ? path : null
  } catch {
    return null
  }
}

function httpsUrl(target: string): URL | null {
  try {
    const url = new URL(target)
    return url.protocol === 'https:' && !url.username && !url.password ? url : null
  } catch {
    return null
  }
}

/**
 * Restrict post-auth redirect candidates to local paths or HTTPS URLs on the platform domain.
 *
 * Platform subdomains include other customers' instances, so the result is only a candidate:
 * approve it with `isPlatformRedirect` or `isInstanceRedirect` before navigating to it.
 */
export function sanitizePostAuthRedirect(
  target: string | null | undefined,
  platformDomain = ''
): string {
  if (!target) {
    return DEFAULT_POST_AUTH_REDIRECT
  }

  if (target.startsWith('/')) {
    return localPath(target) ?? DEFAULT_POST_AUTH_REDIRECT
  }

  const url = httpsUrl(target)
  if (url && isPlatformSubdomain(url.hostname, normalizeDomain(platformDomain))) {
    return url.toString()
  }
  return DEFAULT_POST_AUTH_REDIRECT
}

/** Return whether a redirect stays on the portal or another platform-operated host. */
export function isPlatformRedirect(target: string, platformDomain: string): boolean {
  if (target.startsWith('/')) {
    return localPath(target) !== null
  }
  const url = httpsUrl(target)
  return url !== null && isPlatformOperatedHost(url.host, normalizeDomain(platformDomain))
}

/** Return whether a redirect targets the origin of one of the given instance URLs. */
export function isInstanceRedirect(
  target: string,
  instanceUrls: readonly (string | null | undefined)[]
): boolean {
  const url = httpsUrl(target)
  return (
    url !== null &&
    instanceUrls.some((instanceUrl) => instanceUrl && httpsUrl(instanceUrl)?.origin === url.origin)
  )
}
