const DEFAULT_API_URL = 'http://localhost:8000'

function resolveApiUrl(env = process.env) {
  if (env.PLATFORM_DOMAIN) {
    return `https://api.${env.PLATFORM_DOMAIN}`
  }

  return DEFAULT_API_URL
}

module.exports = {
  DEFAULT_API_URL,
  resolveApiUrl,
}
