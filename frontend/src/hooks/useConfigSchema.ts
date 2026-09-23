import { useCallback, useEffect, useState } from "react";

import { API_ENDPOINTS, fetchJSON } from "@/lib/api";
import { isPlainObject, type JsonSchema } from "@/lib/configSchema";

// The schema only changes with the backend build, so one fetch serves the session.
let cachedSchema: JsonSchema | null = null;
let pendingSchema: Promise<JsonSchema> | null = null;

function loadConfigSchema(): Promise<JsonSchema> {
  pendingSchema ??= fetchJSON<unknown>(API_ENDPOINTS.config.schema)
    .then((schema) => {
      if (
        !isPlainObject(schema) ||
        !isPlainObject(schema.properties) ||
        !isPlainObject(schema.$defs)
      ) {
        throw new Error("Unexpected configuration schema response.");
      }
      cachedSchema = schema as JsonSchema;
      return cachedSchema;
    })
    .finally(() => {
      pendingSchema = null;
    });
  return pendingSchema;
}

export function useConfigSchema(): {
  schema: JsonSchema | null;
  error: string | null;
  retry: () => void;
} {
  const [schema, setSchema] = useState<JsonSchema | null>(cachedSchema);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (schema != null) {
      return;
    }
    let active = true;
    loadConfigSchema().then(
      (loaded) => {
        if (active) {
          setSchema(loaded);
          setError(null);
        }
      },
      (loadError: unknown) => {
        if (active) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "Failed to load the configuration schema.",
          );
        }
      },
    );
    return () => {
      active = false;
    };
  }, [schema, attempt]);

  const retry = useCallback(() => {
    setError(null);
    setAttempt((current) => current + 1);
  }, []);

  return { schema, error, retry };
}
