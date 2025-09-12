// Simple TTL cache for optimistic UI
class TTLCache<T = any> {
  private cache = new Map<string, { data: T; expires: number }>()
  private ttl: number

  constructor(ttlSeconds: number = 300) { // Default 5 minutes
    this.ttl = ttlSeconds * 1000
  }

  get(key: string): T | null {
    const entry = this.cache.get(key)
    if (!entry) return null

    if (Date.now() > entry.expires) {
      this.cache.delete(key)
      return null
    }

    return entry.data
  }

  set(key: string, data: T): void {
    this.cache.set(key, {
      data,
      expires: Date.now() + this.ttl
    })
  }

  delete(key: string): void {
    this.cache.delete(key)
  }

  clear(): void {
    this.cache.clear()
  }
}

// Global cache instance
const cache = new TTLCache()

// Export simple functions for backward compatibility
export function getCached<T>(key: string): T | null {
  return cache.get(key) as T | null
}

export function setCached<T>(key: string, data: T): void {
  cache.set(key, data)
}
