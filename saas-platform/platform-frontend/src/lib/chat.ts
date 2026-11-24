const DEFAULT_CHAT_ORIGIN = 'https://chat.mindroom.chat'

function stripTrailingSlashes(value: string): string {
  return value.replace(/\/+$/, '')
}

export function buildChatLoginUrl(matrixServerUrl: string, chatOrigin = DEFAULT_CHAT_ORIGIN): string {
  const origin = stripTrailingSlashes(chatOrigin.trim())
  const homeserver = stripTrailingSlashes(matrixServerUrl.trim())
  return `${origin}/login/${encodeURIComponent(homeserver)}/`
}
