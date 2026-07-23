const DEFAULT_CINNY_ORIGIN = 'https://chat.mindroom.chat'

function stripTrailingSlashes(value: string): string {
  return value.replace(/\/+$/, '')
}

export function buildCinnyLoginUrl(matrixServerUrl: string, cinnyOrigin = DEFAULT_CINNY_ORIGIN): string {
  const origin = stripTrailingSlashes(cinnyOrigin.trim())
  const homeserver = stripTrailingSlashes(matrixServerUrl.trim())
  return `${origin}/login/${encodeURIComponent(homeserver)}/`
}
