import { listInstances, type Instance } from '@/lib/api'
import { instanceCache } from '@/lib/cache'

const INSTANCE_CACHE_KEY = 'user-instance'

export function getCachedInstance(): Instance | null {
  return instanceCache.get(INSTANCE_CACHE_KEY) as Instance | null
}

export function cacheInstance(instance: Instance | null): void {
  if (instance) {
    instanceCache.set(INSTANCE_CACHE_KEY, instance)
  } else {
    instanceCache.delete(INSTANCE_CACHE_KEY)
  }
}

export async function loadInstance(): Promise<Instance | null> {
  const data = await listInstances()
  const instance = data.instances?.[0] ?? null
  cacheInstance(instance)
  return instance
}
