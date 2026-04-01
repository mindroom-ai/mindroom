import { useEffect, useState } from 'react';
import { Controller, type Control, type Path, useWatch } from 'react-hook-form';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { FieldGroup } from './FieldGroup';
import type { CompactionConfig, Config } from '@/types/config';

type HistoryFieldName = 'num_history_runs' | 'num_history_messages' | 'max_tool_calls_from_history';

type HistoryContextFormValues = {
  num_history_runs?: number | null;
  num_history_messages?: number | null;
  max_tool_calls_from_history?: number | null;
  compress_tool_results?: boolean;
  compaction?: CompactionConfig | null;
};

interface HistoryContextSectionProps<T extends HistoryContextFormValues> {
  control: Control<T>;
  resetKey: string | null | undefined;
  defaults?: Config['defaults'];
  onFieldChange: (fieldName: HistoryFieldName, value: number | boolean | null) => void;
  updateCompaction: (nextCompaction: CompactionConfig | null | undefined) => void;
  mutateCompaction: (
    mutator: (current: CompactionConfig | null | undefined) => CompactionConfig | null | undefined
  ) => void;
  historyRunsHelperText: string;
  historyMessagesHelperText: string;
  maxToolCallsHelperText: string;
  autoCompactionHelperText: string;
  thresholdTokensHelperText: string;
  compactionModelPlaceholder: string;
  numHistoryRunsError?: string;
  numHistoryMessagesError?: string;
  maxToolCallsFromHistoryError?: string;
  compactionError?: string;
  compressToolResults?: {
    defaultValue: boolean;
    helperText: string;
    onChange: (value: boolean) => void;
  };
}

function parseOptionalInt(raw: string, min: number): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') {
    return null;
  }
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
  const trimmed = raw.trim();
  if (trimmed === '') {
    return null;
  }
  const value = Number.parseFloat(trimmed);
  if (!Number.isFinite(value) || value <= 0 || value >= 1) {
    return null;
  }
  return value;
}

export function HistoryContextSection<T extends HistoryContextFormValues>({
  control,
  resetKey,
  defaults,
  onFieldChange,
  updateCompaction,
  mutateCompaction,
  historyRunsHelperText,
  historyMessagesHelperText,
  maxToolCallsHelperText,
  autoCompactionHelperText,
  thresholdTokensHelperText,
  compactionModelPlaceholder,
  numHistoryRunsError,
  numHistoryMessagesError,
  maxToolCallsFromHistoryError,
  compactionError,
  compressToolResults,
}: HistoryContextSectionProps<T>) {
  const numHistoryRuns = useWatch({ name: 'num_history_runs' as Path<T>, control }) as
    | number
    | null
    | undefined;
  const numHistoryMessages = useWatch({
    name: 'num_history_messages' as Path<T>,
    control,
  }) as number | null | undefined;
  const compactionConfig = useWatch({ name: 'compaction' as Path<T>, control }) as
    | CompactionConfig
    | null
    | undefined;
  const [compactionThresholdPercentInput, setCompactionThresholdPercentInput] = useState('');
  const defaultCompaction = defaults?.compaction ?? null;
  const effectiveCompactionEnabled =
    compactionConfig != null
      ? compactionConfig.enabled ?? true
      : defaultCompaction?.enabled ?? false;

  useEffect(() => {
    setCompactionThresholdPercentInput(
      compactionConfig?.threshold_percent != null ? String(compactionConfig.threshold_percent) : ''
    );
  }, [compactionConfig?.threshold_percent, resetKey]);

  return (
    <div className="border-t border-gray-200 dark:border-gray-700 pt-4 mt-2">
      <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-4">
        History & Context
      </h3>

      <div className="space-y-4">
        <FieldGroup
          label="History Runs"
          helperText={historyRunsHelperText}
          htmlFor="num_history_runs"
          error={numHistoryRunsError}
        >
          <Controller
            name={'num_history_runs' as Path<T>}
            control={control}
            render={({ field }) => {
              const fieldValue = field.value as number | null | undefined;
              return (
                <Input
                  id="num_history_runs"
                  type="number"
                  min={1}
                  value={fieldValue ?? ''}
                  placeholder={
                    defaults?.num_history_runs != null
                      ? `Default: ${defaults.num_history_runs}`
                      : 'Default: all'
                  }
                  disabled={numHistoryMessages != null}
                  onChange={e => {
                    const value = parseOptionalInt(e.target.value, 1);
                    field.onChange(value);
                    onFieldChange('num_history_runs', value);
                  }}
                />
              );
            }}
          />
          {numHistoryMessages != null && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              Disabled because History Messages is set (mutually exclusive).
            </p>
          )}
        </FieldGroup>

        <FieldGroup
          label="History Messages"
          helperText={historyMessagesHelperText}
          htmlFor="num_history_messages"
          error={numHistoryMessagesError}
        >
          <Controller
            name={'num_history_messages' as Path<T>}
            control={control}
            render={({ field }) => {
              const fieldValue = field.value as number | null | undefined;
              return (
                <Input
                  id="num_history_messages"
                  type="number"
                  min={1}
                  value={fieldValue ?? ''}
                  placeholder={
                    defaults?.num_history_messages != null
                      ? `Default: ${defaults.num_history_messages}`
                      : 'Default: all'
                  }
                  disabled={numHistoryRuns != null}
                  onChange={e => {
                    const value = parseOptionalInt(e.target.value, 1);
                    field.onChange(value);
                    onFieldChange('num_history_messages', value);
                  }}
                />
              );
            }}
          />
          {numHistoryRuns != null && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              Disabled because History Runs is set (mutually exclusive).
            </p>
          )}
        </FieldGroup>

        <FieldGroup
          label="Max Tool Calls from History"
          helperText={maxToolCallsHelperText}
          htmlFor="max_tool_calls_from_history"
          error={maxToolCallsFromHistoryError}
        >
          <Controller
            name={'max_tool_calls_from_history' as Path<T>}
            control={control}
            render={({ field }) => {
              const fieldValue = field.value as number | null | undefined;
              return (
                <Input
                  id="max_tool_calls_from_history"
                  type="number"
                  min={0}
                  value={fieldValue ?? ''}
                  placeholder={
                    defaults?.max_tool_calls_from_history != null
                      ? `Default: ${defaults.max_tool_calls_from_history}`
                      : 'Default: no limit'
                  }
                  onChange={e => {
                    const value = parseOptionalInt(e.target.value, 0);
                    field.onChange(value);
                    onFieldChange('max_tool_calls_from_history', value);
                  }}
                />
              );
            }}
          />
        </FieldGroup>

        {compressToolResults && (
          <FieldGroup
            label="Compress Tool Results"
            helperText={compressToolResults.helperText}
            htmlFor="compress_tool_results"
          >
            <Controller
              name={'compress_tool_results' as Path<T>}
              control={control}
              render={({ field }) => {
                const fieldValue = field.value as boolean | undefined;
                return (
                  <div className="flex items-center gap-2">
                    <Checkbox
                      id="compress_tool_results"
                      checked={fieldValue ?? compressToolResults.defaultValue}
                      onCheckedChange={checked => {
                        const value = checked === true;
                        field.onChange(value);
                        compressToolResults.onChange(value);
                      }}
                    />
                    <label
                      htmlFor="compress_tool_results"
                      className="text-sm font-medium cursor-pointer select-none"
                    >
                      Compress tool results
                    </label>
                  </div>
                );
              }}
            />
          </FieldGroup>
        )}

        <FieldGroup
          label="Auto-Compaction"
          helperText={autoCompactionHelperText}
          htmlFor="compaction_enabled"
          error={compactionError}
        >
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <Checkbox
                id="compaction_enabled"
                checked={effectiveCompactionEnabled}
                onCheckedChange={checked => {
                  mutateCompaction(current => ({
                    ...(current ?? {}),
                    enabled: checked === true,
                  }));
                }}
              />
              <label
                htmlFor="compaction_enabled"
                className="text-sm font-medium cursor-pointer select-none"
              >
                Enable auto-compaction
              </label>
              {compactionConfig != null && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => updateCompaction(undefined)}
                >
                  Use inherited settings
                </Button>
              )}
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <FieldGroup
                label="Threshold Tokens"
                helperText={thresholdTokensHelperText}
                htmlFor="compaction_threshold_tokens"
              >
                <Input
                  id="compaction_threshold_tokens"
                  type="number"
                  min={1}
                  value={compactionConfig?.threshold_tokens ?? ''}
                  placeholder={
                    defaultCompaction?.threshold_tokens != null
                      ? `Default: ${defaultCompaction.threshold_tokens}`
                      : 'Default: derived from context window'
                  }
                  onChange={e => {
                    const value = parseOptionalInt(e.target.value, 1);
                    mutateCompaction(current => ({
                      ...(current ?? {}),
                      threshold_tokens: value ?? undefined,
                      threshold_percent:
                        value != null ? null : current?.threshold_percent ?? undefined,
                    }));
                  }}
                />
              </FieldGroup>

              <FieldGroup
                label="Threshold Percent"
                helperText="Fraction of the context window that triggers compaction, greater than 0 and less than 1."
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
                      : 'Default: 0.8'
                  }
                  onChange={e => {
                    const raw = e.target.value;
                    setCompactionThresholdPercentInput(raw);
                    const value = parseOptionalUnitFloat(raw);
                    if (raw.trim() !== '' && value == null) {
                      return;
                    }
                    mutateCompaction(current => ({
                      ...(current ?? {}),
                      threshold_percent: value ?? undefined,
                      threshold_tokens:
                        value != null ? null : current?.threshold_tokens ?? undefined,
                    }));
                  }}
                  onBlur={() => {
                    const value = parseOptionalUnitFloat(compactionThresholdPercentInput);
                    if (compactionThresholdPercentInput.trim() !== '' && value == null) {
                      setCompactionThresholdPercentInput(
                        compactionConfig?.threshold_percent != null
                          ? String(compactionConfig.threshold_percent)
                          : ''
                      );
                    }
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
                  value={compactionConfig?.reserve_tokens ?? ''}
                  placeholder={`Default: ${defaultCompaction?.reserve_tokens ?? 16384}`}
                  onChange={e => {
                    const value = parseOptionalInt(e.target.value, 0);
                    mutateCompaction(current => ({
                      ...(current ?? {}),
                      reserve_tokens: value ?? undefined,
                    }));
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
                  value={compactionConfig?.model ?? ''}
                  placeholder={compactionModelPlaceholder}
                  onChange={e => {
                    const value = e.target.value.trim();
                    mutateCompaction(current => ({
                      ...(current ?? {}),
                      model: value === '' ? null : value,
                    }));
                  }}
                />
              </FieldGroup>

              <FieldGroup
                label="Notify Room"
                helperText={`Post a room notice after compaction completes (default: ${
                  defaultCompaction?.notify ? 'on' : 'off'
                }).`}
                htmlFor="compaction_notify"
              >
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="compaction_notify"
                    checked={compactionConfig?.notify ?? defaultCompaction?.notify ?? false}
                    onCheckedChange={checked => {
                      mutateCompaction(current => ({
                        ...(current ?? {}),
                        notify: checked === true,
                      }));
                    }}
                  />
                  <label
                    htmlFor="compaction_notify"
                    className="text-sm font-medium cursor-pointer select-none"
                  >
                    Send compaction notices
                  </label>
                </div>
              </FieldGroup>
            </div>
          </div>
        </FieldGroup>
      </div>
    </div>
  );
}
