import { ExternalLink, Info } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import type { ToolInfo } from "@/hooks/useTools";

const MAX_VISIBLE_FUNCTIONS = 6;
const SAFE_DOCS_URL_PATTERN = /^https?:\/\//i;

interface ToolInfoPopoverProps {
  tool: ToolInfo;
}

function formatMetadataValue(value: string): string {
  return value
    .replace(/_/g, " ")
    .split(" ")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function MetadataRow({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  if (!value) {
    return null;
  }

  return (
    <div className="flex items-center justify-between gap-3 text-xs">
      <span className="font-medium text-muted-foreground">{label}</span>
      <span className="text-right">{formatMetadataValue(value)}</span>
    </div>
  );
}

export function ToolInfoPopover({ tool }: ToolInfoPopoverProps) {
  const detailsLabel = `${tool.display_name} tool details`;
  const docsUrl =
    tool.docs_url != null && SAFE_DOCS_URL_PATTERN.test(tool.docs_url)
      ? tool.docs_url
      : null;
  const functionNames = tool.function_names ?? [];
  const visibleFunctionNames = functionNames.slice(0, MAX_VISIBLE_FUNCTIONS);
  const hiddenFunctionCount =
    functionNames.length - visibleFunctionNames.length;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={`Show ${detailsLabel}`}
          className="h-8 w-8 flex-shrink-0"
        >
          <Info className="h-4 w-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-80 max-w-[calc(100vw-2rem)] space-y-3 p-3"
        role="dialog"
        aria-label={detailsLabel}
      >
        <div className="space-y-1">
          <div className="text-sm font-semibold">{tool.display_name}</div>
          {tool.description && (
            <p className="text-xs leading-relaxed text-muted-foreground">
              {tool.description}
            </p>
          )}
        </div>

        {tool.helper_text && (
          <p className="rounded-md bg-muted/60 px-2 py-1.5 text-xs leading-relaxed text-muted-foreground">
            {tool.helper_text}
          </p>
        )}

        <div className="space-y-1.5">
          <MetadataRow label="Setup" value={tool.setup_type} />
          <MetadataRow label="Status" value={tool.status} />
          <MetadataRow
            label="Default Runtime"
            value={tool.default_execution_target}
          />
        </div>

        {tool.dependencies != null && tool.dependencies.length > 0 && (
          <div className="space-y-1">
            <div className="text-xs font-medium text-muted-foreground">
              Dependencies
            </div>
            <div className="flex flex-wrap gap-1">
              {tool.dependencies.map((dependency) => (
                <Badge
                  key={dependency}
                  variant="outline"
                  className="text-[10px]"
                >
                  {dependency}
                </Badge>
              ))}
            </div>
          </div>
        )}

        {visibleFunctionNames.length > 0 && (
          <div className="space-y-1">
            <div className="text-xs font-medium text-muted-foreground">
              Functions
            </div>
            <div className="flex flex-wrap gap-1">
              {visibleFunctionNames.map((functionName) => (
                <Badge
                  key={functionName}
                  variant="secondary"
                  className="font-mono text-[10px]"
                >
                  {functionName}
                </Badge>
              ))}
              {hiddenFunctionCount > 0 && (
                <Badge variant="outline" className="text-[10px]">
                  +{hiddenFunctionCount} more
                </Badge>
              )}
            </div>
          </div>
        )}

        {docsUrl && (
          <a
            href={docsUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
          >
            Open documentation
            <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </a>
        )}
      </PopoverContent>
    </Popover>
  );
}
