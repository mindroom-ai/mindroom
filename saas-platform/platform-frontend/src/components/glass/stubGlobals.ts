// Test helper standing in for Vitest's vi.stubGlobal, used by the tests ported from MindRoom Chat.
const originals = new Map<string, PropertyDescriptor | undefined>()
const globals = globalThis as Record<string, unknown>

// jest.setup.js defines some globals as writable but not configurable, so those are assigned instead of redefined.
const define = (name: string, descriptor: PropertyDescriptor) => {
  if (Object.getOwnPropertyDescriptor(globalThis, name)?.configurable === false) globals[name] = descriptor.value
  else Object.defineProperty(globalThis, name, descriptor)
}

export const stubGlobal = (name: string, value: unknown) => {
  if (!originals.has(name)) originals.set(name, Object.getOwnPropertyDescriptor(globalThis, name))
  define(name, { configurable: true, writable: true, value })
}

export const unstubAllGlobals = () => {
  originals.forEach((descriptor, name) => {
    if (descriptor) define(name, descriptor)
    else delete globals[name]
  })
  originals.clear()
}
