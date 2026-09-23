import { Button } from "@/components/ui/button";

/** Explain why schema-driven settings are missing and offer another attempt. */
export function SchemaUnavailable({
  subject,
  error,
  onRetry,
}: {
  subject: string;
  error: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-dashed px-4 py-3 text-sm text-muted-foreground">
      <span>
        {subject} are unavailable: {error}
      </span>
      <Button type="button" variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
