// Simple in-memory cache for optimistic UI
const TTL = 5 * 60 * 1000 // 5 minutes
const cache = new Map<string, { data: any; timestamp: number }>()

export function getCached<T>(key: string): T | null {
  const entry = cache.get(key)
  if (!entry) return null

  if (Date.now() - entry.timestamp > TTL) {
    cache.delete(key)
    return null
  }

  return entry.data
}

export function setCached<T>(key: string, data: T): void {
  cache.set(key, { data, timestamp: Date.now() })
}
