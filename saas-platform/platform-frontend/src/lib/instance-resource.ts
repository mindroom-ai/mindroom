import { listInstances, type Instance } from '@/lib/api'
import { instanceCache } from '@/lib/cache'

const pendingLoads = new Map<string, symbol>()

function cacheKey(userId: string): string {
  return `user-instance:${userId}`
}

export function getCachedInstance(userId: string | null): Instance | null {
  return userId ? instanceCache.get(cacheKey(userId)) as Instance | null : null
}

export function cacheInstance(userId: string, instance: Instance | null): void {
  if (instance) {
    instanceCache.set(cacheKey(userId), instance)
  } else {
    instanceCache.delete(cacheKey(userId))
  }
}

export async function loadInstance(userId: string): Promise<Instance | null> {
  const request = Symbol()
  pendingLoads.set(userId, request)
  try {
    const data = await listInstances()
    const instance = data.instances?.[0] ?? null
    if (pendingLoads.get(userId) === request) cacheInstance(userId, instance)
    return instance
  } finally {
    if (pendingLoads.get(userId) === request) pendingLoads.delete(userId)
  }
}
