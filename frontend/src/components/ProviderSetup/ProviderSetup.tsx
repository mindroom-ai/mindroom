import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/toaster";
import { API_BASE_URL, fetchJSON } from "@/lib/api";
import { cn } from "@/lib/utils";

export type ConnectableProvider = "openrouter" | "anthropic" | "openai";

export interface MissingProviderKey {
  provider: string;
  models: string[];
}

interface ProviderSetupStatus {
  missing: MissingProviderKey[];
}

interface ConnectProviderResponse {
  service: string;
  missing: MissingProviderKey[];
}

const STATUS_URL = `${API_BASE_URL}/api/provider-setup/status`;
const CONNECT_URL = `${API_BASE_URL}/api/provider-setup/connect`;

const PROVIDER_OPTIONS: {
  id: ConnectableProvider;
  label: string;
  description: string;
  keyUrl: string;
  placeholder: string;
}[] = [
  {
    id: "openrouter",
    label: "OpenRouter",
    description:
      "One key covers chat, memory, and voice in the default setup, with access to models from every major lab.",
    keyUrl: "https://openrouter.ai/settings/keys",
    placeholder: "sk-or-v1-...",
  },
  {
    id: "anthropic",
    label: "Anthropic",
    description:
      "Claude models directly from Anthropic. Your models must use the Anthropic provider.",
    keyUrl: "https://console.anthropic.com/settings/keys",
    placeholder: "sk-ant-...",
  },
  {
    id: "openai",
    label: "OpenAI",
    description:
      "GPT models directly from OpenAI. Your models must use the OpenAI provider.",
    keyUrl: "https://platform.openai.com/api-keys",
    placeholder: "sk-...",
  },
];

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: "Anthropic",
  azure: "Azure OpenAI",
  cerebras: "Cerebras",
  deepseek: "DeepSeek",
  google: "Google",
  groq: "Groq",
  openai: "OpenAI",
  openrouter: "OpenRouter",
  zai: "Z.ai",
};

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

function listLabel(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** Pick the provider the configured models already use, else OpenRouter. */
export function defaultProviderChoice(
  missing: MissingProviderKey[],
): ConnectableProvider {
  const used = missing.find((entry) =>
    PROVIDER_OPTIONS.some((option) => option.id === entry.provider),
  );
  return (used?.provider as ConnectableProvider | undefined) ?? "openrouter";
}

/**
 * Explain which models still cannot run after a key was saved, for example an
 * Anthropic key while the default models still use OpenRouter.
 */
export function remainingProviderHint(
  connected: ConnectableProvider,
  missing: MissingProviderKey[],
): string | null {
  if (missing.length === 0) return null;
  const models = missing.flatMap((entry) => entry.models);
  const providers = listLabel(
    missing.map((entry) => providerLabel(entry.provider)),
  );
  const modelLabel = models.length === 1 ? "model" : "models";
  return `${providerLabel(connected)} key saved. Your agents still use ${providers} for the ${listLabel(models)} ${modelLabel}. Switch ${models.length === 1 ? "it" : "them"} to ${providerLabel(connected)} on the Models page, or connect ${providers} too.`;
}

export function providerSetupMessage(missing: MissingProviderKey[]): string {
  const providers = listLabel(
    missing.map((entry) => providerLabel(entry.provider)),
  );
  return `Your agents can't reply until MindRoom has an API key for ${providers}.`;
}

function ProviderSetupDialog({
  open,
  onOpenChange,
  missing,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  missing: MissingProviderKey[];
}) {
  const queryClient = useQueryClient();
  const [provider, setProvider] = useState<ConnectableProvider>(() =>
    defaultProviderChoice(missing),
  );
  const [apiKey, setApiKey] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const selected = PROVIDER_OPTIONS.find((option) => option.id === provider)!;

  const selectProvider = (next: ConnectableProvider) => {
    setProvider(next);
    setError(null);
    setHint(null);
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!apiKey.trim()) {
      setError("Paste an API key first.");
      return;
    }
    setIsSaving(true);
    setError(null);
    setHint(null);
    try {
      const result = await fetchJSON<ConnectProviderResponse>(CONNECT_URL, {
        method: "POST",
        body: JSON.stringify({ provider, api_key: apiKey }),
      });
      setApiKey("");
      void queryClient.invalidateQueries({
        queryKey: ["provider-setup-status"],
      });
      const remaining = remainingProviderHint(provider, result.missing);
      if (remaining) {
        setHint(remaining);
        return;
      }
      toast({
        title: `${selected.label} connected`,
        description: "Your agents can reply now.",
      });
      onOpenChange(false);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not save the API key.",
      );
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[560px]">
        <form onSubmit={(event) => void handleSubmit(event)}>
          <DialogHeader>
            <DialogTitle>Connect your AI provider</DialogTitle>
            <DialogDescription>
              Paste an API key from your provider. MindRoom checks it with the
              provider before saving it.
            </DialogDescription>
          </DialogHeader>

          <div
            role="radiogroup"
            aria-label="AI provider"
            className="mt-4 grid gap-2"
          >
            {PROVIDER_OPTIONS.map((option) => (
              <button
                key={option.id}
                type="button"
                role="radio"
                aria-checked={provider === option.id}
                onClick={() => selectProvider(option.id)}
                className={cn(
                  "rounded-lg border px-3 py-2.5 text-left transition-colors",
                  provider === option.id
                    ? "border-primary bg-primary/5"
                    : "border-border hover:bg-muted/50",
                )}
              >
                <span className="flex items-center gap-2 text-sm font-medium">
                  {option.label}
                  {option.id === "openrouter" && (
                    <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs text-primary">
                      Recommended
                    </span>
                  )}
                </span>
                <span className="mt-0.5 block text-xs text-muted-foreground">
                  {option.description}
                </span>
              </button>
            ))}
          </div>

          <div className="mt-4 space-y-2">
            <Label htmlFor="provider-api-key">{selected.label} API key</Label>
            <Input
              id="provider-api-key"
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={selected.placeholder}
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              disabled={isSaving}
            />
            <p className="text-xs text-muted-foreground">
              No key yet?{" "}
              <a
                href={selected.keyUrl}
                target="_blank"
                rel="noreferrer"
                className="text-primary underline-offset-2 hover:underline"
              >
                Get a key from {selected.label}
              </a>
              .
            </p>
          </div>

          {error && (
            <p role="alert" className="mt-3 text-sm text-destructive">
              {error}
            </p>
          )}
          {hint && (
            <div
              role="status"
              className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-900 dark:text-amber-100"
            >
              <p>{hint}</p>
              <Link
                to="/models"
                onClick={() => onOpenChange(false)}
                className="mt-1 inline-block font-medium underline underline-offset-2"
              >
                Open the Models page
              </Link>
            </div>
          )}

          <DialogFooter className="mt-5">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              {hint ? "Close" : "Cancel"}
            </Button>
            <Button type="submit" disabled={isSaving}>
              {isSaving ? "Checking key…" : "Verify and save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Prominent first-run prompt shown while the configured router, agents, or
 * teams use a model whose provider key cannot be resolved.
 */
export function ProviderSetupBanner({ refreshKey }: { refreshKey: string }) {
  const [open, setOpen] = useState(false);
  const status = useQuery({
    queryKey: ["provider-setup-status", refreshKey],
    queryFn: () => fetchJSON<ProviderSetupStatus>(STATUS_URL),
    retry: false,
  });
  const missing = status.data?.missing ?? [];
  if (missing.length === 0 && !open) {
    return null;
  }
  return (
    <>
      {missing.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-primary/20 bg-primary/10 px-3 py-2.5 text-sm sm:px-6">
          <p className="flex items-center gap-2">
            <KeyRound aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span>
              <strong className="font-medium">Connect your AI provider.</strong>{" "}
              {providerSetupMessage(missing)}
            </span>
          </p>
          <Button size="sm" onClick={() => setOpen(true)}>
            Connect provider
          </Button>
        </div>
      )}
      {open && (
        <ProviderSetupDialog
          open={open}
          onOpenChange={setOpen}
          missing={missing}
        />
      )}
    </>
  );
}
