import { useEffect, useState } from 'react';
import { Brain } from 'lucide-react';

import type { Config as MindRoomConfig } from '@/types/config';
import { EditorPanel } from '@/components/shared/EditorPanel';
import { FieldGroup } from '@/components/shared/FieldGroup';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useConfigStore } from '@/store/configStore';

const EMBEDDER_PROVIDERS = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'ollama', label: 'Ollama' },
];

const MEMORY_BACKENDS = [
  { value: 'mem0', label: 'Mem0 (vector)' },
  { value: 'file', label: 'File (markdown)' },
];

const DEFAULT_MODELS: Record<string, string> = {
  openai: 'text-embedding-3-small',
  ollama: 'nomic-embed-text',
};

const DEFAULT_HOSTS: Record<string, string> = {
  openai: '',
  ollama: 'http://localhost:11434',
};

const MODEL_PLACEHOLDERS: Record<string, string> = {
  openai: 'e.g. text-embedding-3-small',
  ollama: 'e.g. nomic-embed-text',
};

type MemorySettings = MindRoomConfig['memory'];

const DEFAULT_MEMORY_SETTINGS: MemorySettings = {
  backend: 'mem0',
  embedder: {
    provider: 'openai',
    config: {
      model: 'text-embedding-3-small',
      host: '',
    },
  },
  file: {
    path: '',
    entrypoint_file: 'MEMORY.md',
    max_entrypoint_lines: 200,
  },
  auto_flush: {
    enabled: false,
    flush_interval_seconds: 180,
    idle_seconds: 120,
    max_dirty_age_seconds: 600,
    stale_ttl_seconds: 86400,
    max_cross_session_reprioritize: 5,
    retry_cooldown_seconds: 30,
    max_retry_cooldown_seconds: 300,
    batch: {
      max_sessions_per_cycle: 10,
      max_sessions_per_agent_per_cycle: 3,
    },
    extractor: {
      no_reply_token: 'NO_REPLY',
      max_messages_per_flush: 20,
      max_chars_per_flush: 12000,
      max_extraction_seconds: 30,
      max_retries: 3,
      include_memory_context: {
        daily_tail_lines: 80,
        memory_snippets: 5,
        snippet_max_chars: 400,
      },
    },
    curation: {
      enabled: false,
      max_lines_per_pass: 20,
      max_passes_per_day: 1,
      append_only: true,
    },
  },
};

function normalizeMemorySettings(memory: MindRoomConfig['memory'] | undefined): MemorySettings {
  const merged: MemorySettings = {
    ...DEFAULT_MEMORY_SETTINGS,
    ...(memory || {}),
    embedder: {
      ...DEFAULT_MEMORY_SETTINGS.embedder,
      ...(memory?.embedder || {}),
      config: {
        ...DEFAULT_MEMORY_SETTINGS.embedder.config,
        ...(memory?.embedder?.config || {}),
      },
    },
    file: {
      ...DEFAULT_MEMORY_SETTINGS.file,
      ...(memory?.file || {}),
    },
    auto_flush: {
      ...DEFAULT_MEMORY_SETTINGS.auto_flush,
      ...(memory?.auto_flush || {}),
      batch: {
        ...DEFAULT_MEMORY_SETTINGS.auto_flush?.batch,
        ...(memory?.auto_flush?.batch || {}),
      },
      extractor: {
        ...DEFAULT_MEMORY_SETTINGS.auto_flush?.extractor,
        ...(memory?.auto_flush?.extractor || {}),
        include_memory_context: {
          ...DEFAULT_MEMORY_SETTINGS.auto_flush?.extractor?.include_memory_context,
          ...(memory?.auto_flush?.extractor?.include_memory_context || {}),
        },
      },
      curation: {
        ...DEFAULT_MEMORY_SETTINGS.auto_flush?.curation,
        ...(memory?.auto_flush?.curation || {}),
      },
    },
  };

  if (!merged.embedder.config.host) {
    merged.embedder.config.host = DEFAULT_HOSTS[merged.embedder.provider] || '';
  }
  return merged;
}

function parseInteger(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}

function parseBoolean(value: string): boolean {
  return value === 'true';
}

export function MemoryConfig() {
  const { config, updateMemoryConfig, saveConfig, isDirty } = useConfigStore();
  const [localConfig, setLocalConfig] = useState<MemorySettings>(() =>
    normalizeMemorySettings(config?.memory)
  );

  useEffect(() => {
    setLocalConfig(normalizeMemorySettings(config?.memory));
  }, [config]);

  const applyMemoryConfig = (nextConfig: MemorySettings) => {
    setLocalConfig(nextConfig);
    updateMemoryConfig(nextConfig);
  };

  const handleBackendChange = (backend: 'mem0' | 'file') => {
    applyMemoryConfig({ ...localConfig, backend });
  };

  const handleProviderChange = (provider: string) => {
    applyMemoryConfig({
      ...localConfig,
      embedder: {
        ...localConfig.embedder,
        provider,
        config: {
          ...localConfig.embedder.config,
          model: DEFAULT_MODELS[provider] || '',
          host: DEFAULT_HOSTS[provider] || '',
        },
      },
    });
  };

  const handleModelChange = (model: string) => {
    applyMemoryConfig({
      ...localConfig,
      embedder: {
        ...localConfig.embedder,
        config: {
          ...localConfig.embedder.config,
          model,
        },
      },
    });
  };

  const handleHostChange = (host: string) => {
    applyMemoryConfig({
      ...localConfig,
      embedder: {
        ...localConfig.embedder,
        config: {
          ...localConfig.embedder.config,
          host,
        },
      },
    });
  };

  const handleFilePathChange = (path: string) => {
    applyMemoryConfig({
      ...localConfig,
      file: {
        ...localConfig.file,
        path,
      },
    });
  };

  const handleAutoFlushEnabled = (enabled: boolean) => {
    applyMemoryConfig({
      ...localConfig,
      auto_flush: {
        ...localConfig.auto_flush,
        enabled,
      },
    });
  };

  const updateAutoFlush = (updates: Partial<NonNullable<MemorySettings['auto_flush']>>) => {
    applyMemoryConfig({
      ...localConfig,
      auto_flush: {
        ...localConfig.auto_flush,
        ...updates,
      },
    });
  };

  const updateAutoFlushBatch = (
    updates: Partial<NonNullable<NonNullable<MemorySettings['auto_flush']>['batch']>>
  ) => {
    updateAutoFlush({
      batch: {
        ...localConfig.auto_flush?.batch,
        ...updates,
      },
    });
  };

  const updateAutoFlushExtractor = (
    updates: Partial<NonNullable<NonNullable<MemorySettings['auto_flush']>['extractor']>>
  ) => {
    updateAutoFlush({
      extractor: {
        ...localConfig.auto_flush?.extractor,
        ...updates,
      },
    });
  };

  const updateAutoFlushExtractorContext = (
    updates: Partial<
      NonNullable<
        NonNullable<
          NonNullable<MemorySettings['auto_flush']>['extractor']
        >['include_memory_context']
      >
    >
  ) => {
    updateAutoFlushExtractor({
      include_memory_context: {
        ...localConfig.auto_flush?.extractor?.include_memory_context,
        ...updates,
      },
    });
  };

  const updateAutoFlushCuration = (
    updates: Partial<NonNullable<NonNullable<MemorySettings['auto_flush']>['curation']>>
  ) => {
    updateAutoFlush({
      curation: {
        ...localConfig.auto_flush?.curation,
        ...updates,
      },
    });
  };

  const handleSave = async () => {
    await saveConfig();
  };

  return (
    <EditorPanel
      icon={Brain}
      title="Memory Configuration"
      isDirty={isDirty}
      onSave={handleSave}
      onDelete={() => {}}
      showActions={true}
      disableDelete={true}
      className="h-full"
    >
      <div className="space-y-6">
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">
            Configure the embedder for agent memory storage and retrieval. You can also choose the
            memory backend and auto-flush behavior.
          </p>
        </div>

        <div className="space-y-4">
          <FieldGroup
            label="Memory Backend"
            helperText="Choose vector memory (mem0) or file-based markdown memory."
            required
            htmlFor="memory-backend"
          >
            <Select
              value={localConfig.backend || 'mem0'}
              onValueChange={value => handleBackendChange(value as 'mem0' | 'file')}
            >
              <SelectTrigger id="memory-backend" className="transition-colors hover:border-ring">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MEMORY_BACKENDS.map(backend => (
                  <SelectItem key={backend.value} value={backend.value}>
                    {backend.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FieldGroup>

          <FieldGroup
            label="Embedder Provider"
            helperText={
              localConfig.embedder.provider === 'ollama'
                ? 'Local embeddings using Ollama'
                : localConfig.embedder.provider === 'openai'
                  ? 'OpenAI or any OpenAI-compatible API (set Base URL below)'
                  : 'Choose your embedding provider'
            }
            required
            htmlFor="provider"
          >
            <Select value={localConfig.embedder.provider} onValueChange={handleProviderChange}>
              <SelectTrigger id="provider" className="transition-colors hover:border-ring">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {EMBEDDER_PROVIDERS.map(provider => (
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
              value={localConfig.embedder.config.model}
              onChange={e => handleModelChange(e.target.value)}
              placeholder={MODEL_PLACEHOLDERS[localConfig.embedder.provider] || 'Model name'}
              className="transition-colors hover:border-ring focus:border-ring"
            />
          </FieldGroup>

          <FieldGroup
            label={localConfig.embedder.provider === 'ollama' ? 'Ollama Host URL' : 'Base URL'}
            helperText={
              localConfig.embedder.provider === 'ollama'
                ? 'The URL where your Ollama server is running'
                : 'Leave empty for official OpenAI API, or set for OpenAI-compatible servers'
            }
            required={localConfig.embedder.provider === 'ollama'}
            htmlFor="host"
          >
            <Input
              id="host"
              type="url"
              value={localConfig.embedder.config.host || ''}
              onChange={e => handleHostChange(e.target.value)}
              placeholder={
                localConfig.embedder.provider === 'ollama'
                  ? 'http://localhost:11434'
                  : 'https://api.openai.com/v1'
              }
              className="transition-colors hover:border-ring focus:border-ring"
            />
          </FieldGroup>

          {localConfig.backend === 'file' && (
            <>
              <FieldGroup
                label="File Memory Path"
                helperText="Directory containing MEMORY.md and auto-flush memory files."
                htmlFor="file-memory-path"
              >
                <Input
                  id="file-memory-path"
                  type="text"
                  value={localConfig.file?.path || ''}
                  onChange={e => handleFilePathChange(e.target.value)}
                  placeholder="./mindroom_data/memory_files"
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Entrypoint File"
                helperText="Primary memory markdown file loaded into context."
                htmlFor="entrypoint-file"
              >
                <Input
                  id="entrypoint-file"
                  type="text"
                  value={localConfig.file?.entrypoint_file || 'MEMORY.md'}
                  onChange={e =>
                    applyMemoryConfig({
                      ...localConfig,
                      file: {
                        ...localConfig.file,
                        entrypoint_file: e.target.value,
                      },
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Entrypoint Max Lines"
                helperText="Maximum lines preloaded from the entrypoint file."
                htmlFor="entrypoint-max-lines"
              >
                <Input
                  id="entrypoint-max-lines"
                  type="number"
                  min={1}
                  value={localConfig.file?.max_entrypoint_lines ?? 200}
                  onChange={e =>
                    applyMemoryConfig({
                      ...localConfig,
                      file: {
                        ...localConfig.file,
                        max_entrypoint_lines: parseInteger(e.target.value, 200),
                      },
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Auto Flush"
                helperText="Automatically persist durable memory from dirty sessions."
                htmlFor="auto-flush-enabled"
              >
                <Select
                  value={String(localConfig.auto_flush?.enabled ?? false)}
                  onValueChange={value => handleAutoFlushEnabled(parseBoolean(value))}
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

              <FieldGroup
                label="Flush Interval (seconds)"
                helperText="Worker cycle interval for background flush processing."
                htmlFor="flush-interval-seconds"
              >
                <Input
                  id="flush-interval-seconds"
                  type="number"
                  min={5}
                  value={localConfig.auto_flush?.flush_interval_seconds ?? 180}
                  onChange={e =>
                    updateAutoFlush({
                      flush_interval_seconds: parseInteger(e.target.value, 180),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Idle Seconds"
                helperText="Minimum idle time before a dirty session is eligible."
                htmlFor="idle-seconds"
              >
                <Input
                  id="idle-seconds"
                  type="number"
                  min={0}
                  value={localConfig.auto_flush?.idle_seconds ?? 120}
                  onChange={e =>
                    updateAutoFlush({
                      idle_seconds: parseInteger(e.target.value, 120),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Max Dirty Age (seconds)"
                helperText="Force flush eligibility after this dirty age."
                htmlFor="max-dirty-age-seconds"
              >
                <Input
                  id="max-dirty-age-seconds"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.max_dirty_age_seconds ?? 600}
                  onChange={e =>
                    updateAutoFlush({
                      max_dirty_age_seconds: parseInteger(e.target.value, 600),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Stale TTL (seconds)"
                helperText="How long to keep inactive dirty-session state entries."
                htmlFor="stale-ttl-seconds"
              >
                <Input
                  id="stale-ttl-seconds"
                  type="number"
                  min={60}
                  value={localConfig.auto_flush?.stale_ttl_seconds ?? 86400}
                  onChange={e =>
                    updateAutoFlush({
                      stale_ttl_seconds: parseInteger(e.target.value, 86400),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Cross-Session Reprioritize"
                helperText="Number of same-agent dirty sessions to boost on incoming prompts."
                htmlFor="max-cross-session-reprioritize"
              >
                <Input
                  id="max-cross-session-reprioritize"
                  type="number"
                  min={0}
                  value={localConfig.auto_flush?.max_cross_session_reprioritize ?? 5}
                  onChange={e =>
                    updateAutoFlush({
                      max_cross_session_reprioritize: parseInteger(e.target.value, 5),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Retry Cooldown (seconds)"
                helperText="Base cooldown before retrying failed extraction."
                htmlFor="retry-cooldown-seconds"
              >
                <Input
                  id="retry-cooldown-seconds"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.retry_cooldown_seconds ?? 30}
                  onChange={e =>
                    updateAutoFlush({
                      retry_cooldown_seconds: parseInteger(e.target.value, 30),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Max Retry Cooldown (seconds)"
                helperText="Upper bound for retry cooldown backoff."
                htmlFor="max-retry-cooldown-seconds"
              >
                <Input
                  id="max-retry-cooldown-seconds"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.max_retry_cooldown_seconds ?? 300}
                  onChange={e =>
                    updateAutoFlush({
                      max_retry_cooldown_seconds: parseInteger(e.target.value, 300),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Batch: Max Sessions Per Cycle"
                helperText="Upper bound of sessions processed in one flush iteration."
                htmlFor="max-sessions-per-cycle"
              >
                <Input
                  id="max-sessions-per-cycle"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.batch?.max_sessions_per_cycle ?? 10}
                  onChange={e =>
                    updateAutoFlushBatch({
                      max_sessions_per_cycle: parseInteger(e.target.value, 10),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Batch: Max Sessions Per Agent"
                helperText="Per-agent cap for each flush iteration."
                htmlFor="max-sessions-per-agent"
              >
                <Input
                  id="max-sessions-per-agent"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.batch?.max_sessions_per_agent_per_cycle ?? 3}
                  onChange={e =>
                    updateAutoFlushBatch({
                      max_sessions_per_agent_per_cycle: parseInteger(e.target.value, 3),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Extractor: Max Messages Per Flush"
                helperText="How much recent chat is considered during one extraction."
                htmlFor="max-messages-per-flush"
              >
                <Input
                  id="max-messages-per-flush"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.extractor?.max_messages_per_flush ?? 20}
                  onChange={e =>
                    updateAutoFlushExtractor({
                      max_messages_per_flush: parseInteger(e.target.value, 20),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Extractor: Max Chars Per Flush"
                helperText="Character cap for chat excerpt passed to extraction."
                htmlFor="max-chars-per-flush"
              >
                <Input
                  id="max-chars-per-flush"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.extractor?.max_chars_per_flush ?? 12000}
                  onChange={e =>
                    updateAutoFlushExtractor({
                      max_chars_per_flush: parseInteger(e.target.value, 12000),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Extractor: Max Duration (seconds)"
                helperText="Timeout for one extraction attempt."
                htmlFor="max-extraction-seconds"
              >
                <Input
                  id="max-extraction-seconds"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.extractor?.max_extraction_seconds ?? 30}
                  onChange={e =>
                    updateAutoFlushExtractor({
                      max_extraction_seconds: parseInteger(e.target.value, 30),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Extractor: Max Retries"
                helperText="Consecutive failures before extended cooldown."
                htmlFor="max-retries"
              >
                <Input
                  id="max-retries"
                  type="number"
                  min={0}
                  value={localConfig.auto_flush?.extractor?.max_retries ?? 3}
                  onChange={e =>
                    updateAutoFlushExtractor({
                      max_retries: parseInteger(e.target.value, 3),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Extractor: NO_REPLY Token"
                helperText="Exact token used by the extractor when nothing should be stored."
                htmlFor="no-reply-token"
              >
                <Input
                  id="no-reply-token"
                  type="text"
                  value={localConfig.auto_flush?.extractor?.no_reply_token ?? 'NO_REPLY'}
                  onChange={e =>
                    updateAutoFlushExtractor({
                      no_reply_token: e.target.value,
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Context: Memory Snippets"
                helperText="How many existing memory snippets to include for dedupe context."
                htmlFor="memory-snippets"
              >
                <Input
                  id="memory-snippets"
                  type="number"
                  min={0}
                  value={
                    localConfig.auto_flush?.extractor?.include_memory_context?.memory_snippets ?? 5
                  }
                  onChange={e =>
                    updateAutoFlushExtractorContext({
                      memory_snippets: parseInteger(e.target.value, 5),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Context: Snippet Max Chars"
                helperText="Character cap per included memory snippet."
                htmlFor="snippet-max-chars"
              >
                <Input
                  id="snippet-max-chars"
                  type="number"
                  min={1}
                  value={
                    localConfig.auto_flush?.extractor?.include_memory_context?.snippet_max_chars ??
                    400
                  }
                  onChange={e =>
                    updateAutoFlushExtractorContext({
                      snippet_max_chars: parseInteger(e.target.value, 400),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Context: Daily Tail Lines"
                helperText="Reserved for daily-note context depth."
                htmlFor="daily-tail-lines"
              >
                <Input
                  id="daily-tail-lines"
                  type="number"
                  min={0}
                  value={
                    localConfig.auto_flush?.extractor?.include_memory_context?.daily_tail_lines ??
                    80
                  }
                  onChange={e =>
                    updateAutoFlushExtractorContext({
                      daily_tail_lines: parseInteger(e.target.value, 80),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Auto Curation"
                helperText="Optional append-only curation from daily memory into MEMORY.md."
                htmlFor="curation-enabled"
              >
                <Select
                  value={String(localConfig.auto_flush?.curation?.enabled ?? false)}
                  onValueChange={value =>
                    updateAutoFlushCuration({
                      enabled: parseBoolean(value),
                    })
                  }
                >
                  <SelectTrigger
                    id="curation-enabled"
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
                label="Curation: Max Lines Per Pass"
                helperText="Maximum lines appended to MEMORY.md per curation pass."
                htmlFor="curation-max-lines"
              >
                <Input
                  id="curation-max-lines"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.curation?.max_lines_per_pass ?? 20}
                  onChange={e =>
                    updateAutoFlushCuration({
                      max_lines_per_pass: parseInteger(e.target.value, 20),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Curation: Max Passes Per Day"
                helperText="Maximum curation runs per day."
                htmlFor="curation-max-passes"
              >
                <Input
                  id="curation-max-passes"
                  type="number"
                  min={1}
                  value={localConfig.auto_flush?.curation?.max_passes_per_day ?? 1}
                  onChange={e =>
                    updateAutoFlushCuration({
                      max_passes_per_day: parseInteger(e.target.value, 1),
                    })
                  }
                  className="transition-colors hover:border-ring focus:border-ring"
                />
              </FieldGroup>

              <FieldGroup
                label="Curation: Append Only"
                helperText="When enabled, curation never auto-deletes from MEMORY.md."
                htmlFor="curation-append-only"
              >
                <Select
                  value={String(localConfig.auto_flush?.curation?.append_only ?? true)}
                  onValueChange={value =>
                    updateAutoFlushCuration({
                      append_only: parseBoolean(value),
                    })
                  }
                >
                  <SelectTrigger
                    id="curation-append-only"
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
            </>
          )}
        </div>

        {localConfig.backend !== 'file' &&
          localConfig.embedder.provider === 'openai' &&
          !localConfig.embedder.config.host && (
            <div className="p-4 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800/30 rounded-lg shadow-sm">
              <p className="text-sm text-yellow-800 dark:text-yellow-300">
                <strong>Note:</strong> You&apos;ll need to set the OPENAI_API_KEY environment
                variable for this provider to work.
              </p>
            </div>
          )}

        <div className="p-4 bg-muted/50 rounded-lg shadow-sm border border-border">
          <h3 className="text-sm font-medium mb-3">Current Configuration</h3>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Backend:</span>
              <span className="font-mono text-foreground">{localConfig.backend || 'mem0'}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Provider:</span>
              <span className="font-mono text-foreground">{localConfig.embedder.provider}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Model:</span>
              <span className="font-mono text-foreground">{localConfig.embedder.config.model}</span>
            </div>
            {localConfig.backend === 'file' && (
              <>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Auto Flush:</span>
                  <span className="font-mono text-foreground">
                    {localConfig.auto_flush?.enabled ? 'enabled' : 'disabled'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Batch Size:</span>
                  <span className="font-mono text-foreground">
                    {localConfig.auto_flush?.batch?.max_sessions_per_cycle || 10}
                  </span>
                </div>
              </>
            )}
            {localConfig.embedder.config.host && (
              <div className="flex justify-between">
                <span className="text-muted-foreground">
                  {localConfig.embedder.provider === 'ollama' ? 'Host:' : 'Base URL:'}
                </span>
                <span className="font-mono text-foreground">
                  {localConfig.embedder.config.host}
                </span>
              </div>
            )}
          </div>
        </div>
      </div>
    </EditorPanel>
  );
}
