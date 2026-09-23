import { useEffect, useSyncExternalStore } from "react";

import { API_ENDPOINTS, fetchJSON } from "@/lib/api";
import { isPlainObject, type JsonSchema } from "@/lib/configSchema";

interface ConfigSchemaState {
  schema: JsonSchema | null;
  error: string | null;
}

// One load status for the whole session, shared by every form that renders
// from the schema, so a single retry updates them all.
let state: ConfigSchemaState = { schema: null, error: null };
let pending = false;
const listeners = new Set<() => void>();

function publish(next: ConfigSchemaState): void {
  state = next;
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function loadConfigSchema(): void {
  if (pending || state.schema != null) {
    return;
  }
  pending = true;
  fetchJSON<unknown>(API_ENDPOINTS.config.schema)
    .then((schema) => {
      if (
        !isPlainObject(schema) ||
        !isPlainObject(schema.properties) ||
        !isPlainObject(schema.$defs)
      ) {
        throw new Error("Unexpected configuration schema response.");
      }
      publish({ schema: schema as JsonSchema, error: null });
    })
    .catch((error: unknown) => {
      publish({
        schema: null,
        error:
          error instanceof Error
            ? error.message
            : "Failed to load the configuration schema.",
      });
    })
    .finally(() => {
      pending = false;
    });
}

function retry(): void {
  publish({ schema: null, error: null });
  loadConfigSchema();
}

export function useConfigSchema(): ConfigSchemaState & { retry: () => void } {
  const current = useSyncExternalStore(subscribe, () => state);
  useEffect(() => {
    if (current.schema == null && current.error == null) {
      loadConfigSchema();
    }
  }, [current]);
  return { ...current, retry };
}
