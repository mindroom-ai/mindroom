import { ChevronDown, ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { useConfigSchema } from "@/hooks/useConfigSchema";
import { definitionSchema, fieldLabel } from "@/lib/configSchema";
import { getConfigValidationIssues } from "@/lib/configValidation";
import { useConfigStore } from "@/store/configStore";

import { useOpenOnError, type SchemaPath } from "./inputs";
import { SchemaFields, schemaFieldKeys } from "./SchemaField";
import { SchemaUnavailable } from "./SchemaUnavailable";

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
  const { schema: root, error, retry } = useConfigSchema();
  const { diagnostics } = useConfigStore();
  const schema = root?.$defs?.[definition];
  const keys =
    root != null && schema != null
      ? schemaFieldKeys(schema, root, { exclude })
      : [];
  const hasErrors =
    keys.length > 0 &&
    getConfigValidationIssues(diagnostics).some(
      (issue) =>
        path.every((segment, index) => issue.loc[index] === segment) &&
        keys.includes(String(issue.loc[path.length])),
    );
  const [open, setOpen] = useOpenOnError(defaultOpen, hasErrors);

  if (error != null) {
    return <SchemaUnavailable subject={title} error={error} onRetry={retry} />;
  }
  // A backend without this definition has no fields to add here.
  if (root == null || schema == null || keys.length === 0) {
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
            {hasErrors && <Badge variant="destructive">Needs attention</Badge>}
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
            schema={definitionSchema(root, definition)}
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
