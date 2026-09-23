import { useEffect, useState } from "react";

import { API_ENDPOINTS, fetchJSON } from "@/lib/api";
import type { JsonSchema } from "@/lib/configSchema";

// The schema only changes with the backend build, so one fetch serves the session.
let cachedSchema: JsonSchema | null = null;
let pendingSchema: Promise<JsonSchema> | null = null;

function loadConfigSchema(): Promise<JsonSchema> {
  pendingSchema ??= fetchJSON<JsonSchema>(API_ENDPOINTS.config.schema)
    .then((schema) => {
      cachedSchema = schema;
      return schema;
    })
    .finally(() => {
      pendingSchema = null;
    });
  return pendingSchema;
}

export function useConfigSchema(): {
  schema: JsonSchema | null;
  error: string | null;
} {
  const [schema, setSchema] = useState<JsonSchema | null>(cachedSchema);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (schema != null) {
      return;
    }
    let active = true;
    loadConfigSchema().then(
      (loaded) => {
        if (active) {
          setSchema(loaded);
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
  }, [schema]);

  return { schema, error };
}
