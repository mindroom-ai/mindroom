import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { useConfigSchema } from "@/hooks/useConfigSchema";
import { definitionSchema, fieldLabel } from "@/lib/configSchema";

import { SchemaFields, schemaFieldKeys, type SchemaPath } from "./SchemaField";

export interface SchemaSectionProps {
  title: string;
  /** Name of the schema definition under $defs, such as "AgentConfig". */
  definition: string;
  value: unknown;
  onFieldChange: (key: string, next: unknown) => void;
  path: SchemaPath;
  /** Keys the surrounding hand-built editor already renders. */
  exclude?: readonly string[];
  defaultOpen?: boolean;
}

/** Collapsible form for every key of one schema object that a hand-built editor does not render. */
export function SchemaSection({
  title,
  definition,
  value,
  onFieldChange,
  path,
  exclude,
  defaultOpen = false,
}: SchemaSectionProps) {
  const { schema: root, error } = useConfigSchema();
  const [open, setOpen] = useState(defaultOpen);

  if (error != null) {
    return (
      <p className="rounded-lg border border-dashed px-4 py-3 text-sm text-muted-foreground">
        {title} are unavailable: {error}
      </p>
    );
  }
  // A backend without this definition has no fields to add here.
  if (root?.$defs?.[definition] == null) {
    return null;
  }

  const schema = definitionSchema(root, definition);
  const keys = schemaFieldKeys(schema, root, { exclude });
  if (keys.length === 0) {
    return null;
  }
  const Icon = open ? ChevronDown : ChevronRight;

  return (
    <section className="rounded-lg border border-border/60">
      <button
        type="button"
        className="flex w-full items-start gap-2 px-4 py-3 text-left"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2 text-sm font-semibold">
            {title}
            <Badge variant="secondary">{keys.length}</Badge>
          </span>
          {!open && (
            <span className="mt-1 block truncate text-xs text-muted-foreground">
              {keys.map(fieldLabel).join(", ")}
            </span>
          )}
        </span>
      </button>
      {open && (
        <div className="border-t border-border/60 px-4 py-4">
          <SchemaFields
            schema={schema}
            root={root}
            value={value}
            path={path}
            exclude={exclude}
            onFieldChange={onFieldChange}
          />
        </div>
      )}
    </section>
  );
}
