import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { SchemaSection } from "@/components/SchemaForm";
import { setObjectKey } from "@/lib/configSchema";
import { FieldGroup } from "./FieldGroup";
import { useScopedConfigValidation } from "@/hooks/useScopedConfigValidation";
import {
  resolveEffectiveCompactionEnabled,
  type CompactionConfig,
  type Config,
  type Team,
} from "@/types/config";

// Compaction keys this section renders by hand; More compaction settings shows the rest.
const COMPACTION_EDITOR_FIELDS = [
  "enabled",
  "threshold_tokens",
  "threshold_percent",
  "replay_window_tokens",
  "reserve_tokens",
  "model",
] as const;

const THRESHOLD_TOKENS_HELPER_TEXT =
  "Compaction trigger in tokens, counted over the whole request. Supported OpenAI and Claude routes compact natively at it; every other route runs text compaction before the reply.";

/** The history and compaction fields agents and teams share. */
type HistoryContextValues = Pick<
  Team,
  | "num_history_runs"
  | "num_history_messages"
  | "max_tool_calls_from_history"
  | "compaction"
>;

interface HistoryContextSectionProps {
  values: HistoryContextValues;
  onChange: <K extends keyof HistoryContextValues>(
    field: K,
    value: HistoryContextValues[K],
  ) => void;
  resetKey: string | null | undefined;
  /** Where this entity lives in config.yaml, for validation errors. */
  path: Array<string | number>;
  defaults?: Config["defaults"];
  /** Lead sentences; the section appends the inherited default. */
  historyRunsHelperText: string;
  historyMessagesHelperText: string;
  maxToolCallsHelperText: string;
  autoCompactionHelperText: string;
  compactionModelPlaceholder: string;
}

function parseOptionalInt(raw: string, min: number): number | null {
  const trimmed = raw.trim();
  if (!/^-?\d+$/.test(trimmed)) {
    return null;
  }
  const value = Number.parseInt(trimmed, 10);
  if (!Number.isFinite(value) || value < min) {
    return null;
  }
  return value;
}

function parseOptionalUnitFloat(raw: string): number | null {
  const value = Number.parseFloat(raw.trim());
  if (!Number.isFinite(value) || value <= 0 || value >= 1) {
    return null;
  }
  return value;
}

function isInvalidUnitFloatInput(raw: string): boolean {
  return raw.trim() !== "" && parseOptionalUnitFloat(raw) == null;
}

function inheritedDefaultText(
  value: number | null | undefined,
  fallback: string,
): string {
  return ` Leave empty to use default (${value ?? fallback}).`;
}

export function HistoryContextSection({
  values,
  onChange,
  resetKey,
  path,
  defaults,
  historyRunsHelperText,
  historyMessagesHelperText,
  maxToolCallsHelperText,
  autoCompactionHelperText,
  compactionModelPlaceholder,
}: HistoryContextSectionProps) {
  const {
    num_history_runs: numHistoryRuns,
    num_history_messages: numHistoryMessages,
    max_tool_calls_from_history: maxToolCallsFromHistory,
    compaction: compactionConfig,
  } = values;
  const validationErrorForPath = useScopedConfigValidation(path);
  const patchCompaction = (patch: CompactionConfig) =>
    onChange("compaction", { ...compactionConfig, ...patch });
  const [compactionThresholdPercentInput, setCompactionThresholdPercentInput] =
    useState("");
  const defaultCompaction = defaults?.compaction;
  const effectiveCompactionEnabled = resolveEffectiveCompactionEnabled(
    compactionConfig,
    defaultCompaction,
  );

  const storedThresholdPercentInput =
    compactionConfig?.threshold_percent != null
      ? String(compactionConfig.threshold_percent)
      : "";

  useEffect(() => {
    setCompactionThresholdPercentInput(storedThresholdPercentInput);
  }, [storedThresholdPercentInput, resetKey]);

  return (
    <div className="border-t border-gray-200 dark:border-gray-700 pt-4 mt-2">
      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-4">
        History & Context
      </h3>

      <div className="space-y-4">
        <FieldGroup
          label="History Runs"
          helperText={
            historyRunsHelperText +
            inheritedDefaultText(defaults?.num_history_runs, "all")
          }
          htmlFor="num_history_runs"
          error={validationErrorForPath(["num_history_runs"], true)}
        >
          <Input
            id="num_history_runs"
            type="number"
            min={1}
            value={numHistoryRuns ?? ""}
            placeholder={
              defaults?.num_history_runs != null
                ? `Default: ${defaults.num_history_runs}`
                : "Default: all"
            }
            disabled={numHistoryMessages != null}
            onChange={(e) =>
              onChange("num_history_runs", parseOptionalInt(e.target.value, 1))
            }
          />
          {numHistoryMessages != null && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              Disabled because History Messages is set (mutually exclusive).
            </p>
          )}
        </FieldGroup>

        <FieldGroup
          label="History Messages"
          helperText={
            historyMessagesHelperText +
            inheritedDefaultText(defaults?.num_history_messages, "all")
          }
          htmlFor="num_history_messages"
          error={validationErrorForPath(["num_history_messages"], true)}
        >
          <Input
            id="num_history_messages"
            type="number"
            min={1}
            value={numHistoryMessages ?? ""}
            placeholder={
              defaults?.num_history_messages != null
                ? `Default: ${defaults.num_history_messages}`
                : "Default: all"
            }
            disabled={numHistoryRuns != null}
            onChange={(e) =>
              onChange(
                "num_history_messages",
                parseOptionalInt(e.target.value, 1),
              )
            }
          />
          {numHistoryRuns != null && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              Disabled because History Runs is set (mutually exclusive).
            </p>
          )}
        </FieldGroup>

        <FieldGroup
          label="Max Tool Calls from History"
          helperText={
            maxToolCallsHelperText +
            inheritedDefaultText(
              defaults?.max_tool_calls_from_history,
              "no limit",
            )
          }
          htmlFor="max_tool_calls_from_history"
          error={validationErrorForPath(["max_tool_calls_from_history"], true)}
        >
          <Input
            id="max_tool_calls_from_history"
            type="number"
            min={0}
            value={maxToolCallsFromHistory ?? ""}
            placeholder={
              defaults?.max_tool_calls_from_history != null
                ? `Default: ${defaults.max_tool_calls_from_history}`
                : "Default: no limit"
            }
            onChange={(e) =>
              onChange(
                "max_tool_calls_from_history",
                parseOptionalInt(e.target.value, 0),
              )
            }
          />
        </FieldGroup>

        <FieldGroup
          label="Required Compaction"
          helperText={autoCompactionHelperText}
          htmlFor="compaction_enabled"
          error={validationErrorForPath(["compaction"], true)}
        >
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <Checkbox
                id="compaction_enabled"
                checked={effectiveCompactionEnabled}
                onCheckedChange={(checked) => {
                  patchCompaction({ enabled: checked === true });
                }}
              />
              <label
                htmlFor="compaction_enabled"
                className="text-sm font-medium cursor-pointer select-none"
              >
                Enable automatic required compaction
              </label>
              {compactionConfig != null && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => onChange("compaction", undefined)}
                >
                  Use inherited settings
                </Button>
              )}
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <FieldGroup
                label="Threshold Tokens"
                helperText={THRESHOLD_TOKENS_HELPER_TEXT}
                htmlFor="compaction_threshold_tokens"
              >
                <Input
                  id="compaction_threshold_tokens"
                  type="number"
                  min={1}
                  value={compactionConfig?.threshold_tokens ?? ""}
                  placeholder={
                    defaultCompaction?.threshold_tokens != null
                      ? `Default: ${defaultCompaction.threshold_tokens}`
                      : "Default: derived from replay window"
                  }
                  onChange={(e) => {
                    const value = parseOptionalInt(e.target.value, 1);
                    patchCompaction({
                      threshold_tokens: value ?? undefined,
                      threshold_percent:
                        value != null
                          ? null
                          : (compactionConfig?.threshold_percent ?? undefined),
                    });
                  }}
                />
              </FieldGroup>

              <FieldGroup
                label="Threshold Percent"
                helperText="The same compaction trigger as a fraction of the effective replay window, counted over the whole request."
                htmlFor="compaction_threshold_percent"
              >
                <Input
                  id="compaction_threshold_percent"
                  type="number"
                  min={0.01}
                  max={0.99}
                  step="0.01"
                  value={compactionThresholdPercentInput}
                  placeholder={
                    defaultCompaction?.threshold_percent != null
                      ? `Default: ${defaultCompaction.threshold_percent}`
                      : "Default: 0.8"
                  }
                  onChange={(e) => {
                    const raw = e.target.value;
                    setCompactionThresholdPercentInput(raw);
                    if (isInvalidUnitFloatInput(raw)) {
                      return;
                    }
                    const value = parseOptionalUnitFloat(raw);
                    patchCompaction({
                      threshold_percent: value ?? undefined,
                      threshold_tokens:
                        value != null
                          ? null
                          : (compactionConfig?.threshold_tokens ?? undefined),
                    });
                  }}
                  onBlur={() => {
                    if (
                      isInvalidUnitFloatInput(compactionThresholdPercentInput)
                    ) {
                      setCompactionThresholdPercentInput(
                        storedThresholdPercentInput,
                      );
                    }
                  }}
                />
              </FieldGroup>

              <FieldGroup
                label="Replay Window Tokens"
                helperText="Optional persisted-replay and compaction-planning cap. This does not lower the model provider's request limit."
                htmlFor="compaction_replay_window_tokens"
              >
                <Input
                  id="compaction_replay_window_tokens"
                  type="number"
                  min={1}
                  value={compactionConfig?.replay_window_tokens ?? ""}
                  placeholder={
                    defaultCompaction?.replay_window_tokens != null
                      ? `Default: ${defaultCompaction.replay_window_tokens}`
                      : "Default: model context window"
                  }
                  onChange={(e) => {
                    const value = parseOptionalInt(e.target.value, 1);
                    patchCompaction({
                      replay_window_tokens: value ?? undefined,
                    });
                  }}
                />
              </FieldGroup>

              <FieldGroup
                label="Reserve Tokens"
                helperText="Headroom reserved for the current prompt, tools, and model output."
                htmlFor="compaction_reserve_tokens"
              >
                <Input
                  id="compaction_reserve_tokens"
                  type="number"
                  min={0}
                  value={compactionConfig?.reserve_tokens ?? ""}
                  placeholder={`Default: ${defaultCompaction?.reserve_tokens ?? 16384}`}
                  onChange={(e) => {
                    const value = parseOptionalInt(e.target.value, 0);
                    patchCompaction({ reserve_tokens: value ?? undefined });
                  }}
                />
              </FieldGroup>

              <FieldGroup
                label="Compaction Model"
                helperText="Optional model config name used only for summary generation during compaction. Leave blank to clear an inherited compaction model."
                htmlFor="compaction_model"
              >
                <Input
                  id="compaction_model"
                  value={compactionConfig?.model ?? ""}
                  placeholder={compactionModelPlaceholder}
                  onChange={(e) => {
                    const value = e.target.value.trim();
                    patchCompaction({ model: value === "" ? null : value });
                  }}
                />
              </FieldGroup>
            </div>

            <SchemaSection
              title="More compaction settings"
              definition="CompactionOverrideConfig"
              value={compactionConfig}
              path={[...path, "compaction"]}
              exclude={COMPACTION_EDITOR_FIELDS}
              onFieldChange={(key, next) =>
                onChange(
                  "compaction",
                  setObjectKey(compactionConfig, key, next),
                )
              }
            />
          </div>
        </FieldGroup>
      </div>
    </div>
  );
}
