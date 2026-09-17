import type { RefObject } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { TokenTotals } from "@/types/usage";
import { UsageActivity } from "./UsageActivity";
import {
  formatTokens,
  TOKEN_METRICS,
  UsageMetricSelect,
  UsageTable,
} from "./UsageTable";
import type { UsageDetailData } from "./usageDetails";

export function UsageDetail({
  detail,
  generatedAt,
  metric,
  onMetricChange,
  onClose,
  trigger,
  fallbackFocus,
}: {
  detail: UsageDetailData | undefined;
  generatedAt: string;
  metric: keyof TokenTotals;
  onMetricChange: (metric: keyof TokenTotals) => void;
  onClose: () => void;
  trigger: RefObject<HTMLButtonElement | null>;
  fallbackFocus: RefObject<HTMLInputElement | null>;
}) {
  return (
    <Dialog
      open={Boolean(detail)}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        className="max-h-[90vh] w-[calc(100%-2rem)] max-w-3xl grid-cols-1 overflow-y-auto p-4 sm:p-6"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          // A completed refresh can remove the selected row while this is open.
          onClose();
          const target = trigger.current?.isConnected
            ? trigger.current
            : fallbackFocus.current;
          target?.focus();
        }}
      >
        {detail && (
          <>
            <DialogHeader className="text-left">
              <DialogTitle className="pr-5 [overflow-wrap:anywhere]">
                {detail.title}
              </DialogTitle>
              <DialogDescription>{detail.subtitle}</DialogDescription>
            </DialogHeader>
            <section
              aria-label={detail.basis}
              className="rounded-lg border bg-muted/30 p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-xs font-medium text-muted-foreground">
                    {detail.basis}
                  </p>
                  <p className="mt-2 break-words text-3xl font-semibold tracking-tight tabular-nums">
                    {formatTokens(detail.totals[metric])}
                  </p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {TOKEN_METRICS[metric].toLowerCase()}
                  </p>
                </div>
                <p className="rounded-md border bg-background px-3 py-1.5 text-xs text-muted-foreground">
                  {formatTokens(detail.count)} {detail.countLabel}
                </p>
              </div>
              <p className="mt-3 text-xs text-muted-foreground">
                Daily and requester detail may cover less usage than all-time
                totals.
              </p>
            </section>
            <UsageMetricSelect metric={metric} onChange={onMetricChange} />
            <Tabs defaultValue={detail.sections[0].label}>
              <TabsList
                aria-label="Usage detail"
                className="grid h-auto w-full grid-cols-3"
              >
                {[
                  ...detail.sections.map((section) => section.label),
                  "Daily activity",
                ].map((label) => (
                  <TabsTrigger
                    key={label}
                    value={label}
                    className="h-full min-w-0 whitespace-normal px-2 text-xs sm:text-sm"
                  >
                    {label}
                  </TabsTrigger>
                ))}
              </TabsList>
              {detail.sections.map((section) => (
                <TabsContent key={section.label} value={section.label}>
                  <p className="px-1 py-3 text-xs text-muted-foreground">
                    {section.note}
                  </p>
                  <UsageTable
                    rows={section.rows}
                    label={section.label}
                    metric={metric}
                    countLabel={section.countLabel}
                  />
                </TabsContent>
              ))}
              <TabsContent value="Daily activity" className="mt-4">
                <UsageActivity
                  daily={detail.daily}
                  generatedAt={generatedAt}
                  metric={metric}
                />
              </TabsContent>
            </Tabs>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
