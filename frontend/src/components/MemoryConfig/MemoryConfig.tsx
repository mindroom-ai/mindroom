import { Brain } from "lucide-react";

import type {
  Config as MindRoomConfig,
  MemoryBackend,
  MemorySearchMode,
} from "@/types/config";
import { EditorPanel } from "@/components/shared/EditorPanel";
import { FieldGroup } from "@/components/shared/FieldGroup";
import { ParsedInput } from "@/components/shared/ParsedInput";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useConfigStore } from "@/store/configStore";
import { SchemaSection } from "@/components/SchemaForm";

const EMBEDDER_PROVIDERS = [
  { value: "openai", label: "OpenAI" },
  { value: "ollama", label: "Ollama" },
  { value: "sentence_transformers", label: "Sentence Transformers" },
];

const MEMORY_BACKENDS = [
  { value: "mem0", label: "Mem0 (vector)" },
  { value: "file", label: "File (markdown)" },
  { value: "none", label: "Disabled (stateless)" },
];

const DEFAULT_MODELS: Record<string, string> = {
  openai: "text-embedding-3-small",
  ollama: "nomic-embed-text",
  sentence_transformers: "sentence-transformers/all-MiniLM-L6-v2",
};

type MemorySettings = MindRoomConfig["memory"];

/** MemoryConfig keys this page renders by hand; More settings shows the rest. */
const MEMORY_EDITOR_FIELDS = [
  "backend",
  "embedder",
  "file",
  "search",
  "auto_flush",
] as const;

const EMBEDDER_EDITOR_FIELDS = [
  "model",
  "credentials_service",
  "host",
] as const;

const DEFAULT_MEMORY_SETTINGS: MemorySettings = {
  backend: "mem0",
  embedder: {
    provider: "openai",
    config: {
      model: "text-embedding-3-small",
      host: "",
    },
  },
  file: {
    max_entrypoint_lines: 200,
    max_entrypoint_tokens: 50000,
  },
  search: {
    mode: "keyword",
    include: ["memory/**/*.md"],
    include_entrypoint: false,
  },
  auto_flush: {
    enabled: false,
    flush_interval_seconds: 300,
    idle_seconds: 120,
    stale_ttl_seconds: 86400,
    retry_cooldown_seconds: 30,
    max_retry_cooldown_seconds: 300,
    batch: {
      max_sessions_per_cycle: 10,
      max_sessions_per_agent_per_cycle: 3,
    },
    extractor: {
      no_reply_token: "NO_REPLY",
      max_messages_per_flush: 20,
      max_chars_per_flush: 12000,
      max_extraction_seconds: 30,
      include_memory_context: {
        memory_snippets: 5,
        snippet_max_chars: 400,
      },
    },
  },
};

function normalizeMemorySettings(
  memory: MindRoomConfig["memory"] | undefined,
): MemorySettings {
  return {
    ...DEFAULT_MEMORY_SETTINGS,
    ...memory,
    embedder: {
      ...DEFAULT_MEMORY_SETTINGS.embedder,
      ...memory?.embedder,
      config: {
        ...DEFAULT_MEMORY_SETTINGS.embedder.config,
        ...memory?.embedder?.config,
      },
    },
    file: {
      ...DEFAULT_MEMORY_SETTINGS.file,
      ...memory?.file,
    },
    search: {
      ...DEFAULT_MEMORY_SETTINGS.search,
      ...memory?.search,
    },
    auto_flush: {
      ...DEFAULT_MEMORY_SETTINGS.auto_flush,
      ...memory?.auto_flush,
      batch: {
        ...DEFAULT_MEMORY_SETTINGS.auto_flush?.batch,
        ...memory?.auto_flush?.batch,
      },
      extractor: {
        ...DEFAULT_MEMORY_SETTINGS.auto_flush?.extractor,
        ...memory?.auto_flush?.extractor,
        include_memory_context: {
          ...DEFAULT_MEMORY_SETTINGS.auto_flush?.extractor
            ?.include_memory_context,
          ...memory?.auto_flush?.extractor?.include_memory_context,
        },
      },
    },
  };
}

function parseIncludePatterns(value: string): string[] {
  return value
    .split(",")
    .map((pattern) => pattern.trim())
    .filter((pattern) => pattern.length > 0);
}

function providerHelperText(provider: string): string {
  if (provider === "ollama") {
    return "Local embeddings using Ollama";
  }
  if (provider === "openai") {
    return "OpenAI or any OpenAI-compatible API (set Base URL below)";
  }
  if (provider === "sentence_transformers") {
    return "Fully local embeddings using the sentence-transformers Python runtime";
  }
  return "Choose your embedding provider";
}

function NumberField({
  id,
  label,
  helperText,
  min,
  value,
  fallback,
  onChange,
}: {
  id: string;
  label: string;
  helperText: string;
  min: number;
  value: number | undefined;
  fallback: number;
  onChange: (value: number) => void;
}) {
  return (
    <FieldGroup label={label} helperText={helperText} htmlFor={id}>
      <ParsedInput
        id={id}
        type="number"
        min={min}
        format={String}
        value={value ?? fallback}
        parse={(text) => {
          const parsed = Number.parseInt(text, 10);
          return Number.isNaN(parsed) ? fallback : parsed;
        }}
        onChange={onChange}
        className="transition-colors hover:border-ring focus:border-ring"
      />
    </FieldGroup>
  );
}

export function MemoryConfig() {
  const {
    config,
    updateMemoryConfig,
    updateConfigValue,
    saveConfig,
    isDirty,
    isLoading,
  } = useConfigStore();
  const memory = normalizeMemorySettings(config?.memory);

  const handleProviderChange = (provider: string) => {
    updateMemoryConfig({
      ...memory,
      embedder: {
        ...memory.embedder,
        provider,
        // An empty host lets the runtime pick the server, such as OLLAMA_HOST for Ollama.
        config: { model: DEFAULT_MODELS[provider] || "", host: "" },
      },
    });
  };

  const updateEmbedderConfig = (
    updates: Partial<MemorySettings["embedder"]["config"]>,
  ) => {
    updateMemoryConfig({
      ...memory,
      embedder: {
        ...memory.embedder,
        config: {
          ...memory.embedder.config,
          ...updates,
        },
      },
    });
  };

  const handleCredentialsServiceChange = (credentialsService: string) => {
    const embedderConfig = { ...memory.embedder.config };
    const normalizedService = credentialsService.trim();
    if (normalizedService) {
      embedderConfig.credentials_service = normalizedService;
    } else {
      delete embedderConfig.credentials_service;
    }
    updateMemoryConfig({
      ...memory,
      embedder: {
        ...memory.embedder,
        config: embedderConfig,
      },
    });
  };

  const updateSection = <K extends "file" | "search" | "auto_flush">(
    key: K,
    updates: Partial<NonNullable<MemorySettings[K]>>,
  ) => {
    updateMemoryConfig({
      ...memory,
      [key]: {
        ...memory[key],
        ...updates,
      },
    });
  };

  const updateAutoFlushBatch = (
    updates: Partial<
      NonNullable<NonNullable<MemorySettings["auto_flush"]>["batch"]>
    >,
  ) => {
    updateSection("auto_flush", {
      batch: {
        ...memory.auto_flush?.batch,
        ...updates,
      },
    });
  };

  const updateAutoFlushExtractor = (
    updates: Partial<
      NonNullable<NonNullable<MemorySettings["auto_flush"]>["extractor"]>
    >,
  ) => {
    updateSection("auto_flush", {
      extractor: {
        ...memory.auto_flush?.extractor,
        ...updates,
      },
    });
  };

  const updateAutoFlushExtractorContext = (
    updates: Partial<
      NonNullable<
        NonNullable<
          NonNullable<MemorySettings["auto_flush"]>["extractor"]
        >["include_memory_context"]
      >
    >,
  ) => {
    updateAutoFlushExtractor({
      include_memory_context: {
        ...memory.auto_flush?.extractor?.include_memory_context,
        ...updates,
      },
    });
  };

  return (
    <EditorPanel
      icon={Brain}
      title="Memory Configuration"
      isDirty={isDirty}
      onSave={saveConfig}
      onDelete={() => {}}
      disableSave={isLoading}
      disableDelete={true}
      className="h-full"
    >
      <div className="space-y-6">
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">
            Configure the embedder for agent memory storage and retrieval. You
            can also choose the memory backend and auto-flush behavior.
          </p>
        </div>

        <div className="space-y-4">
          <FieldGroup
            label="Memory Backend"
            helperText="Choose vector memory, file-based markdown memory, or disabled memory."
            required
            htmlFor="memory-backend"
          >
            <Select
              value={memory.backend || "mem0"}
              onValueChange={(value) =>
                updateMemoryConfig({
                  ...memory,
                  backend: value as MemoryBackend,
                })
              }
            >
              <SelectTrigger
                id="memory-backend"
                className="transition-colors hover:border-ring"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MEMORY_BACKENDS.map((backend) => (
                  <SelectItem key={backend.value} value={backend.value}>
                    {backend.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FieldGroup>

          <FieldGroup
            label="Embedder Provider"
            helperText={providerHelperText(memory.embedder.provider)}
            required
            htmlFor="provider"
          >
            <Select
              value={memory.embedder.provider}
              onValueChange={handleProviderChange}
            >
              <SelectTrigger
                id="provider"
                className="transition-colors hover:border-ring"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {EMBEDDER_PROVIDERS.map((provider) => (
                  <SelectItem key={provider.value} value={provider.value}>
                    {provider.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FieldGroup>

          <FieldGroup
            label="Embedding Model"
            helperText="The model used to generate embeddings for memory storage"
            required
            htmlFor="model"
          >
            <Input
              id="model"
              type="text"
              value={memory.embedder.config.model}
              onChange={(e) => updateEmbedderConfig({ model: e.target.value })}
              placeholder={
                DEFAULT_MODELS[memory.embedder.provider]
                  ? `e.g. ${DEFAULT_MODELS[memory.embedder.provider]}`
                  : "Model name"
              }
              className="transition-colors hover:border-ring focus:border-ring"
            />
          </FieldGroup>

          {memory.embedder.provider === "openai" && (
            <FieldGroup
              label="Credential Service"
              helperText="Bind this embedder to a credential service. Leave empty for the legacy embedder-to-openai fallback."
              htmlFor="credentials-service"
            >
              <Input
                id="credentials-service"
                type="text"
                value={memory.embedder.config.credentials_service || ""}
                onChange={(e) => handleCredentialsServiceChange(e.target.value)}
                placeholder="embedder"
                className="transition-colors hover:border-ring focus:border-ring"
              />
            </FieldGroup>
          )}

          {memory.embedder.provider !== "sentence_transformers" && (
            <FieldGroup
              label={
                memory.embedder.provider === "ollama"
                  ? "Ollama Host URL"
                  : "Base URL"
              }
              helperText={
                memory.embedder.provider === "ollama"
                  ? "Empty uses OLLAMA_HOST, else http://localhost:11434. A configured OLLAMA_HOST also takes precedence over this URL."
                  : "Empty uses OPENAI_BASE_URL, else the official OpenAI API. Set it for an OpenAI-compatible server."
              }
              htmlFor="host"
            >
              <Input
                id="host"
                type="url"
                value={memory.embedder.config.host || ""}
                onChange={(e) => updateEmbedderConfig({ host: e.target.value })}
                placeholder={
                  memory.embedder.provider === "ollama"
                    ? "http://localhost:11434"
                    : "https://api.openai.com/v1"
                }
                className="transition-colors hover:border-ring focus:border-ring"
              />
            </FieldGroup>
          )}

          <SchemaSection
            title="More embedder settings"
            definition="EmbedderConfig"
            value={config?.memory?.embedder?.config}
            path={["memory", "embedder", "config"]}
            exclude={EMBEDDER_EDITOR_FIELDS}
            // Edit the authored config directly so untouched defaults stay unwritten.
            onFieldChange={(key, next) =>
              updateConfigValue(["memory", "embedder", "config", key], next)
            }
          />

          {memory.backend === "file" && (
            <>
              <NumberField
                id="entrypoint-max-lines"
                label="Entrypoint Max Lines"
                helperText="Maximum lines preloaded from the entrypoint file."
                min={1}
                value={memory.file?.max_entrypoint_lines}
                fallback={200}
                onChange={(value) =>
                  updateSection("file", { max_entrypoint_lines: value })
                }
              />

              <NumberField
                id="entrypoint-max-tokens"
                label="Entrypoint Max Tokens"
                helperText="Maximum tokens, as the turn's model counts them, of whole lines preloaded from the entrypoint file."
                min={1}
                value={memory.file?.max_entrypoint_tokens}
                fallback={50000}
                onChange={(value) =>
                  updateSection("file", { max_entrypoint_tokens: value })
                }
              />

              <FieldGroup
                label="Search Mode"
                helperText="Keyword search is exact text matching; semantic search uses the memory embedder."
                htmlFor="memory-search-mode"
              >
                <Select
                  value={memory.search?.mode ?? "keyword"}
                  onValueChange={(value) =>
                    updateSection("search", { mode: value as MemorySearchMode })
                  }
                >
                  <SelectTrigger
                    id="memory-search-mode"
                    className="transition-colors hover:border-ring"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="keyword">Keyword</SelectItem>
                    <SelectItem value="semantic">Semantic</SelectItem>
                  </SelectContent>
                </Select>
              </FieldGroup>

              <FieldGroup
                label="Search Include"
                helperText="Comma-separated root-relative globs for file-memory search."
                htmlFor="memory-search-include"
              >
                <ParsedInput
                  id="memory-search-include"
                  type="text"
                  value={memory.search?.include || []}
                  format={(patterns) => patterns.join(", ")}
                  parse={parseIncludePatterns}
                  onChange={(include) => updateSection("search", { include })}
                  placeholder="memory/**/*.md"
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Include Entrypoint"
                helperText="Include MEMORY.md in search results in addition to preloaded context."
                htmlFor="memory-search-entrypoint"
              >
                <Select
                  value={String(memory.search?.include_entrypoint ?? false)}
                  onValueChange={(value) =>
                    updateSection("search", {
                      include_entrypoint: value === "true",
                    })
                  }
                >
                  <SelectTrigger
                    id="memory-search-entrypoint"
                    className="transition-colors hover:border-ring"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="true">Enabled</SelectItem>
                    <SelectItem value="false">Disabled</SelectItem>
                  </SelectContent>
                </Select>
              </FieldGroup>

              <FieldGroup
                label="Auto Flush"
                helperText="Automatically save durable memories from the new turns of conversations."
                htmlFor="auto-flush-enabled"
              >
                <Select
                  value={String(memory.auto_flush?.enabled ?? false)}
                  onValueChange={(value) =>
                    updateSection("auto_flush", { enabled: value === "true" })
                  }
                >
                  <SelectTrigger
                    id="auto-flush-enabled"
                    className="transition-colors hover:border-ring"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="true">Enabled</SelectItem>
                    <SelectItem value="false">Disabled</SelectItem>
                  </SelectContent>
                </Select>
              </FieldGroup>

              <NumberField
                id="flush-interval-seconds"
                label="Flush Interval (seconds)"
                helperText="Time between background flush passes."
                min={5}
                value={memory.auto_flush?.flush_interval_seconds}
                fallback={300}
                onChange={(value) =>
                  updateSection("auto_flush", { flush_interval_seconds: value })
                }
              />

              <NumberField
                id="idle-seconds"
                label="Idle Seconds"
                helperText="How long a conversation must be quiet before its new turns are flushed."
                min={0}
                value={memory.auto_flush?.idle_seconds}
                fallback={120}
                onChange={(value) =>
                  updateSection("auto_flush", { idle_seconds: value })
                }
              />

              <NumberField
                id="stale-ttl-seconds"
                label="Stale TTL (seconds)"
                helperText="Give up on turns whose flush has kept failing for this long."
                min={60}
                value={memory.auto_flush?.stale_ttl_seconds}
                fallback={86400}
                onChange={(value) =>
                  updateSection("auto_flush", { stale_ttl_seconds: value })
                }
              />

              <NumberField
                id="retry-cooldown-seconds"
                label="Retry Cooldown (seconds)"
                helperText="Base cooldown before retrying a failed flush."
                min={1}
                value={memory.auto_flush?.retry_cooldown_seconds}
                fallback={30}
                onChange={(value) =>
                  updateSection("auto_flush", { retry_cooldown_seconds: value })
                }
              />

              <NumberField
                id="max-retry-cooldown-seconds"
                label="Max Retry Cooldown (seconds)"
                helperText="Upper bound for retry cooldown backoff."
                min={1}
                value={memory.auto_flush?.max_retry_cooldown_seconds}
                fallback={300}
                onChange={(value) =>
                  updateSection("auto_flush", {
                    max_retry_cooldown_seconds: value,
                  })
                }
              />

              <NumberField
                id="max-sessions-per-cycle"
                label="Batch: Max Sessions Per Cycle"
                helperText="Conversations flushed in one pass, across all agents."
                min={1}
                value={memory.auto_flush?.batch?.max_sessions_per_cycle}
                fallback={10}
                onChange={(value) =>
                  updateAutoFlushBatch({ max_sessions_per_cycle: value })
                }
              />

              <NumberField
                id="max-sessions-per-agent"
                label="Batch: Max Sessions Per Agent"
                helperText="Conversations of one agent or private instance flushed in one pass."
                min={1}
                value={
                  memory.auto_flush?.batch?.max_sessions_per_agent_per_cycle
                }
                fallback={3}
                onChange={(value) =>
                  updateAutoFlushBatch({
                    max_sessions_per_agent_per_cycle: value,
                  })
                }
              />

              <NumberField
                id="max-messages-per-flush"
                label="Extractor: Max Messages Per Flush"
                helperText="New messages read by one extraction; a longer backlog is read over later passes."
                min={1}
                value={memory.auto_flush?.extractor?.max_messages_per_flush}
                fallback={20}
                onChange={(value) =>
                  updateAutoFlushExtractor({ max_messages_per_flush: value })
                }
              />

              <NumberField
                id="max-chars-per-flush"
                label="Extractor: Max Chars Per Flush"
                helperText="Character cap for chat excerpt passed to extraction."
                min={1}
                value={memory.auto_flush?.extractor?.max_chars_per_flush}
                fallback={12000}
                onChange={(value) =>
                  updateAutoFlushExtractor({ max_chars_per_flush: value })
                }
              />

              <NumberField
                id="max-extraction-seconds"
                label="Extractor: Max Duration (seconds)"
                helperText="Timeout for one extraction attempt."
                min={1}
                value={memory.auto_flush?.extractor?.max_extraction_seconds}
                fallback={30}
                onChange={(value) =>
                  updateAutoFlushExtractor({ max_extraction_seconds: value })
                }
              />

              <FieldGroup
                label="Extractor: NO_REPLY Token"
                helperText="Exact token used by the extractor when nothing should be stored."
                htmlFor="no-reply-token"
              >
                <Input
                  id="no-reply-token"
                  type="text"
                  value={
                    memory.auto_flush?.extractor?.no_reply_token ?? "NO_REPLY"
                  }
                  onChange={(e) =>
                    updateAutoFlushExtractor({
                      no_reply_token: e.target.value,
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <NumberField
                id="memory-snippets"
                label="Context: Memory Snippets"
                helperText="How many existing memory snippets to include for dedupe context."
                min={0}
                value={
                  memory.auto_flush?.extractor?.include_memory_context
                    ?.memory_snippets
                }
                fallback={5}
                onChange={(value) =>
                  updateAutoFlushExtractorContext({ memory_snippets: value })
                }
              />

              <NumberField
                id="snippet-max-chars"
                label="Context: Snippet Max Chars"
                helperText="Character cap per included memory snippet."
                min={1}
                value={
                  memory.auto_flush?.extractor?.include_memory_context
                    ?.snippet_max_chars
                }
                fallback={400}
                onChange={(value) =>
                  updateAutoFlushExtractorContext({ snippet_max_chars: value })
                }
              />
            </>
          )}

          <SchemaSection
            title="More settings"
            definition="MemoryConfig"
            value={config?.memory}
            path={["memory"]}
            exclude={MEMORY_EDITOR_FIELDS}
            onFieldChange={(key, next) =>
              updateConfigValue(["memory", key], next)
            }
          />
        </div>

        <div className="p-4 bg-muted/50 rounded-lg border border-border">
          <h3 className="text-sm font-medium mb-3">Current Configuration</h3>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Backend:</span>
              <span className="font-mono text-foreground">
                {memory.backend || "mem0"}
              </span>
            </div>
            {memory.embedder.config.credentials_service && (
              <div className="flex justify-between">
                <span className="text-muted-foreground">
                  Credential Service:
                </span>
                <span className="font-mono text-foreground">
                  {memory.embedder.config.credentials_service}
                </span>
              </div>
            )}
            <div className="flex justify-between">
              <span className="text-muted-foreground">Provider:</span>
              <span className="font-mono text-foreground">
                {memory.embedder.provider}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Model:</span>
              <span className="font-mono text-foreground">
                {memory.embedder.config.model}
              </span>
            </div>
            {memory.backend === "file" && (
              <>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Auto Flush:</span>
                  <span className="font-mono text-foreground">
                    {memory.auto_flush?.enabled ? "enabled" : "disabled"}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Batch Size:</span>
                  <span className="font-mono text-foreground">
                    {memory.auto_flush?.batch?.max_sessions_per_cycle || 10}
                  </span>
                </div>
              </>
            )}
            {memory.embedder.config.host && (
              <div className="flex justify-between">
                <span className="text-muted-foreground">
                  {memory.embedder.provider === "ollama"
                    ? "Host:"
                    : "Base URL:"}
                </span>
                <span className="font-mono text-foreground">
                  {memory.embedder.config.host}
                </span>
              </div>
            )}
          </div>
        </div>
      </div>
    </EditorPanel>
  );
}
