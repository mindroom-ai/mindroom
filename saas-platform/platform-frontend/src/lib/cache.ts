// Simple in-memory cache for optimistic UI
// Data persists across route changes but not page refreshes

interface CacheEntry<T> {
  data: T
  timestamp: number
}

class SimpleCache {
  private cache: Map<string, CacheEntry<any>> = new Map()
  private ttl = 5 * 60 * 1000 // 5 minutes

  get<T>(key: string): T | null {
    const entry = this.cache.get(key)
    if (!entry) return null

    // Check if expired
    if (Date.now() - entry.timestamp > this.ttl) {
      this.cache.delete(key)
      return null
    }

    return entry.data
  }

  set<T>(key: string, data: T): void {
    this.cache.set(key, {
      data,
      timestamp: Date.now()
    })
  }

  clear(): void {
    this.cache.clear()
  }
}

// Singleton instance
export const cache = new SimpleCache()
