import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, Save, Wallet } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useToast } from '@/components/ui/use-toast';
import { NativeSelect } from '@/components/SchemaForm';
import { showSaveFailureToastIfNeeded } from '@/components/shared';
import type { ConfigPath } from '@/lib/configSchema';
import { isConcreteMatrixUserId } from '@/lib/matrixIds';
import { fetchBudgets } from '@/services/budgetService';
import { useConfigStore } from '@/store/configStore';
import type { EnabledBudgetStatus } from '@/types/budgets';
import type { BudgetsConfig, Config, ModelPricing } from '@/types/config';

const USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
});
// Small caps and spend, common while testing, would round to a misleading cent.
const SMALL_USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

function formatUsd(value: number): string {
  return value > 0 && value < 1 ? SMALL_USD.format(value) : USD.format(value);
}
const TOKENS = new Intl.NumberFormat('en-US');
const PRICE_FIELDS = [
  ['input', 'Input'],
  ['output', 'Output'],
  ['cache_read', 'Cache read'],
  ['cache_write', 'Cache write'],
] as const satisfies readonly (readonly [keyof ModelPricing, string])[];

function parseUsd(text: string): number | undefined | null {
  if (text.trim() === '') return undefined;
  const value = Number(text);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

/** A dollar amount input that keeps partial typing like "10." while committing parsed values. */
function UsdInput({
  label,
  value,
  placeholder,
  onCommit,
  className,
}: {
  label: string;
  value: number | null | undefined;
  placeholder?: string;
  onCommit: (value: number | undefined) => void;
  className?: string;
}) {
  const [text, setText] = useState(value == null ? '' : String(value));
  useEffect(() => {
    const parsed = parseUsd(text);
    if ((parsed ?? undefined) !== (value ?? undefined)) {
      setText(value == null ? '' : String(value));
    }
    // Only an outside change of the committed value resets the typed text.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <Input
      type="number"
      inputMode="decimal"
      min={0}
      step="any"
      aria-label={label}
      value={text}
      placeholder={placeholder}
      className={className}
      onChange={event => {
        setText(event.target.value);
        const parsed = parseUsd(event.target.value);
        if (parsed !== null) onCommit(parsed);
      }}
    />
  );
}

function cheapestPricedModel(models: Config['models']): string | undefined {
  const priced = Object.entries(models)
    .filter(([, model]) => model.pricing != null)
    .sort(
      ([, a], [, b]) =>
        (a.pricing?.input ?? 0) +
        (a.pricing?.output ?? 0) -
        ((b.pricing?.input ?? 0) + (b.pricing?.output ?? 0))
    );
  return priced[0]?.[0] ?? Object.keys(models).sort()[0];
}

function SettingsCard({
  config,
  budgets,
  updateConfigValue,
}: {
  config: Config;
  budgets: BudgetsConfig | null;
  updateConfigValue: (path: ConfigPath, value: unknown) => void;
}) {
  const modelOptions = Object.keys(config.models)
    .sort()
    .map(name => ({ value: name, label: name }));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Monthly caps</CardTitle>
        <CardDescription>
          Caps apply per Matrix user to spend in the current UTC month. A user at their cap keeps
          chatting, but replies that would use a priced model use the fallback model instead.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {budgets == null ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">
              Budgets are off, so every user can use every model.
            </p>
            <Button
              type="button"
              onClick={() =>
                updateConfigValue(['budgets'], {
                  fallback_model: cheapestPricedModel(config.models),
                })
              }
              disabled={Object.keys(config.models).length === 0}
            >
              Turn on budgets
            </Button>
          </div>
        ) : (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="space-y-1.5 text-sm font-medium">
                <span>Default cap (USD)</span>
                <UsdInput
                  label="Default monthly cap"
                  value={budgets.monthly_limit_usd}
                  placeholder="Uncapped"
                  onCommit={value => updateConfigValue(['budgets', 'monthly_limit_usd'], value)}
                />
                <span className="block text-xs font-normal text-muted-foreground">
                  Leave blank to cap only the users listed below.
                </span>
              </label>
              <div className="space-y-1.5 text-sm font-medium">
                <span>Fallback model</span>
                <NativeSelect
                  label="Fallback model"
                  value={budgets.fallback_model}
                  options={modelOptions}
                  onChange={value => updateConfigValue(['budgets', 'fallback_model'], value)}
                />
                <span className="block text-xs font-normal text-muted-foreground">
                  Pick the cheapest model that is good enough to keep working.
                </span>
              </div>
            </div>
            <Button
              type="button"
              variant="outline"
              onClick={() => updateConfigValue(['budgets'], undefined)}
            >
              Turn off budgets
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function UsersCard({
  budgets,
  aliases,
  status,
  statusError,
  updateConfigValue,
}: {
  budgets: BudgetsConfig;
  aliases: Record<string, string[]>;
  status: EnabledBudgetStatus | undefined;
  statusError: string | undefined;
  updateConfigValue: (path: ConfigPath, value: unknown) => void;
}) {
  const { toast } = useToast();
  const [newUserId, setNewUserId] = useState('');
  const [newUserCap, setNewUserCap] = useState<number | undefined>();
  // Users edited on this page stay listed, so clearing a cap to retype it keeps the row.
  const [editedUserIds, setEditedUserIds] = useState<ReadonlySet<string>>(() => new Set());
  const overrides = budgets.users ?? {};
  const canonicalUserIds = new Map(
    Object.entries(aliases).flatMap(([canonical, aliasIds]) =>
      aliasIds.map(aliasId => [aliasId, canonical] as const)
    )
  );
  const canonicalOf = (userId: string) => canonicalUserIds.get(userId) ?? userId;
  // Spend is reported per canonical user, so a cap keyed by a bridge alias belongs to that user's row.
  const capKeys = new Map<string, string>();
  for (const key of Object.keys(overrides)) {
    if (!capKeys.has(canonicalOf(key))) capKeys.set(canonicalOf(key), key);
  }
  const capKeyOf = (userId: string) => capKeys.get(userId) ?? userId;
  const spend = new Map((status?.users ?? []).map(user => [user.user_id, user.spend_usd]));
  const userIds = [...new Set([...spend.keys(), ...capKeys.keys(), ...editedUserIds])].sort(
    (a, b) => (spend.get(b) ?? 0) - (spend.get(a) ?? 0) || a.localeCompare(b)
  );
  const defaultLabel =
    budgets.monthly_limit_usd == null
      ? 'Uncapped'
      : `Default (${formatUsd(budgets.monthly_limit_usd)})`;

  const handleAdd = () => {
    const userId = newUserId.trim();
    if (!isConcreteMatrixUserId(userId)) {
      toast({
        title: 'Invalid Matrix user ID',
        description: 'Use a full Matrix user ID like @alice:example.com.',
        variant: 'destructive',
      });
      return;
    }
    if (newUserCap === undefined) {
      toast({
        title: 'Enter a cap',
        description: `Set a monthly cap in USD for ${userId}.`,
        variant: 'destructive',
      });
      return;
    }
    // An alias shares its user's cap, so it updates that user's existing entry.
    const canonicalUserId = canonicalOf(userId);
    updateConfigValue(['budgets', 'users', capKeyOf(canonicalUserId)], newUserCap);
    setEditedUserIds(previous => new Set(previous).add(canonicalUserId));
    setNewUserId('');
    setNewUserCap(undefined);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Users</CardTitle>
        <CardDescription>
          {status != null
            ? `Spend from ${status.period_start} until ${status.period_end}${
                status.generated_at
                  ? `, updated ${new Date(status.generated_at).toLocaleString()}`
                  : ', updating'
              }.`
            : 'Month-to-date spend for users with spend or their own cap.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {statusError != null && (
          <p role="status" className="text-sm text-muted-foreground">
            {statusError}
          </p>
        )}
        <div>
          <table className="w-full text-sm">
            <thead className="hidden text-left text-xs text-muted-foreground sm:table-header-group">
              <tr>
                <th className="py-2 pr-3 font-medium">User</th>
                <th className="py-2 pr-3 font-medium">Spend</th>
                <th className="py-2 pr-3 font-medium">Cap (USD)</th>
                <th className="py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {userIds.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-4 text-muted-foreground">
                    No spend this month yet.
                  </td>
                </tr>
              )}
              {userIds.map(userId => {
                const userSpend = spend.get(userId) ?? 0;
                const override = overrides[capKeyOf(userId)];
                const limit = override ?? budgets.monthly_limit_usd;
                const over = limit != null && userSpend >= limit;
                const fraction =
                  limit == null ? 0 : limit === 0 ? 1 : Math.min(userSpend / limit, 1);
                return (
                  <tr
                    key={userId}
                    className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border/60 py-3 sm:table-row sm:py-0"
                  >
                    <td className="w-full break-all font-mono sm:w-auto sm:py-2 sm:pr-3">
                      {userId}
                    </td>
                    <td className="sm:py-2 sm:pr-3">
                      <div>{formatUsd(userSpend)}</div>
                      {limit != null && (
                        <div
                          aria-hidden="true"
                          className="mt-1 h-1.5 w-24 overflow-hidden rounded-full bg-muted"
                        >
                          <div
                            className={over ? 'h-full bg-destructive' : 'h-full bg-primary'}
                            style={{ width: `${fraction * 100}%` }}
                          />
                        </div>
                      )}
                    </td>
                    <td className="sm:py-2 sm:pr-3">
                      <UsdInput
                        label={`Monthly cap for ${userId}`}
                        value={override}
                        placeholder={defaultLabel}
                        className="w-40"
                        onCommit={value => {
                          setEditedUserIds(previous => new Set(previous).add(userId));
                          updateConfigValue(['budgets', 'users', capKeyOf(userId)], value);
                        }}
                      />
                    </td>
                    <td className="sm:py-2">
                      {over ? (
                        <Badge variant="destructive">
                          Over budget, using {budgets.fallback_model}
                        </Badge>
                      ) : (
                        <Badge variant="secondary">
                          {limit == null ? 'Uncapped' : 'Within budget'}
                        </Badge>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            value={newUserId}
            onChange={event => setNewUserId(event.target.value)}
            placeholder="@user:example.com"
            aria-label="New user Matrix ID"
            className="font-mono sm:max-w-xs"
          />
          <UsdInput
            label="New user cap"
            value={newUserCap}
            placeholder="Cap (USD)"
            className="sm:w-40"
            onCommit={setNewUserCap}
          />
          <Button type="button" variant="outline" onClick={handleAdd}>
            Add user
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function ModelPricesCard({
  config,
  updateConfigValue,
}: {
  config: Config;
  updateConfigValue: (path: ConfigPath, value: unknown) => void;
}) {
  const setPrice = (modelName: string, field: keyof ModelPricing, value: number | undefined) => {
    const next: ModelPricing = { ...(config.models[modelName].pricing ?? {}) };
    if (value === undefined) {
      delete next[field];
    } else {
      next[field] = value;
    }
    const empty = Object.values(next).every(price => price == null);
    updateConfigValue(['models', modelName, 'pricing'], empty ? undefined : next);
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Model prices</CardTitle>
        <CardDescription>
          USD per million tokens. Input and output prices are required for a priced model; cache
          prices default to the input price. Models without prices are free for budgets and are
          never swapped for the fallback.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <table className="w-full text-sm">
          <thead className="hidden text-left text-xs text-muted-foreground sm:table-header-group">
            <tr>
              <th className="py-2 pr-3 font-medium">Model</th>
              {PRICE_FIELDS.map(([field, label]) => (
                <th key={field} className="py-2 pr-3 font-medium">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Object.keys(config.models)
              .sort()
              .map(modelName => {
                const model = config.models[modelName];
                return (
                  <tr
                    key={modelName}
                    className="grid grid-cols-2 gap-x-3 gap-y-2 border-t border-border/60 py-3 sm:table-row sm:py-0"
                  >
                    <td className="col-span-2 sm:py-2 sm:pr-3">
                      <div className="font-medium">{modelName}</div>
                      <div className="font-mono text-xs text-muted-foreground">
                        {model.provider} / {model.id}
                      </div>
                      {model.pricing != null &&
                        (model.pricing.input == null || model.pricing.output == null) && (
                          <div className="text-xs text-destructive">
                            Set both input and output prices.
                          </div>
                        )}
                    </td>
                    {PRICE_FIELDS.map(([field, label]) => (
                      <td key={field} className="sm:py-2 sm:pr-3">
                        <span
                          aria-hidden="true"
                          className="mb-1 block text-xs text-muted-foreground sm:hidden"
                        >
                          {label}
                        </span>
                        <UsdInput
                          label={`${modelName} ${label.toLowerCase()} price`}
                          value={model.pricing?.[field]}
                          placeholder={field.startsWith('cache') ? '= input' : '-'}
                          className="w-full sm:w-24"
                          onCommit={value => setPrice(modelName, field, value)}
                        />
                      </td>
                    ))}
                  </tr>
                );
              })}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

export function Budgets() {
  const { config, isDirty, isLoading, saveConfig, updateConfigValue } = useConfigStore();
  const { toast } = useToast();
  const query = useQuery({
    queryKey: ['budgets'],
    queryFn: ({ signal }) => fetchBudgets(signal),
    retry: false,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
  });
  const status = query.data?.enabled === true ? query.data : undefined;
  const budgets = config?.budgets ?? null;

  const handleSave = async () => {
    const result = await saveConfig();
    if (
      showSaveFailureToastIfNeeded(result, {
        staleMessage: 'Save was superseded by newer budget edits.',
        fallbackMessage: 'Failed to save budgets.',
      })
    ) {
      return;
    }
    toast({
      title: 'Budgets Saved',
      description: 'New caps and prices apply from the next spend refresh.',
    });
  };

  return (
    <div className="mx-auto w-full max-w-6xl space-y-6 p-2 pb-8 sm:p-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-center gap-2">
          <Wallet className="h-5 w-5 text-primary" aria-hidden="true" />
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Budgets</h1>
            <p className="mt-1 text-sm text-muted-foreground">Monthly spending caps per user</p>
          </div>
        </div>
        <Button onClick={handleSave} disabled={!isDirty || isLoading || !config}>
          <Save className="mr-2 h-4 w-4" aria-hidden="true" />
          Save
        </Button>
      </div>
      {config == null ? (
        <p className="text-sm text-muted-foreground">Loading configuration...</p>
      ) : (
        <>
          <SettingsCard config={config} budgets={budgets} updateConfigValue={updateConfigValue} />
          {budgets != null && (
            <UsersCard
              budgets={budgets}
              aliases={config.authorization?.aliases ?? {}}
              status={status}
              statusError={query.isError ? query.error.message : undefined}
              updateConfigValue={updateConfigValue}
            />
          )}
          {status != null && status.unpriced_models.length > 0 && (
            <Alert>
              <AlertTriangle className="h-4 w-4" aria-hidden="true" />
              <AlertTitle>Usage without prices</AlertTitle>
              <AlertDescription>
                <p>
                  These models were used this month but have no prices, so their usage counts as
                  free:
                </p>
                <ul className="mt-2 list-disc pl-5">
                  {status.unpriced_models.map(row => (
                    <li key={`${row.provider}:${row.model}`}>
                      {row.provider} {row.model}: {TOKENS.format(row.total_tokens)} tokens
                    </li>
                  ))}
                </ul>
              </AlertDescription>
            </Alert>
          )}
          <ModelPricesCard config={config} updateConfigValue={updateConfigValue} />
        </>
      )}
    </div>
  );
}
