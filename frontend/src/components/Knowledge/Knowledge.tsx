import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { apiErrorMessageFromPayload, API_ENDPOINTS } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useConfigStore } from "@/store/configStore";
import type { KnowledgeBaseConfig, KnowledgeGitConfig } from "@/types/config";
import { useToast } from "@/components/ui/use-toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  FolderOpen,
  GitBranch,
  Plus,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";

type KnowledgeSourceType = "local" | "git";

interface KnowledgeFile {
  name: string;
  path: string;
  size: number;
  modified: string;
  type: string;
}

interface KnowledgeStatus {
  base_id: string;
  folder_path: string;
  watch: boolean;
  file_count: number;
  indexed_count: number;
  refreshing?: boolean;
  advisory_state?: "none" | "stale" | "refreshing" | "refresh_failed";
  refresh_job?: "idle" | "pending" | "running" | "failed";
  last_error?: string | null;
  file_listing_degraded?: boolean;
  file_listing_error?: string | null;
  git?: {
    repo_url: string;
    branch: string;
    lfs: boolean;
    startup_behavior: "blocking" | "background";
    syncing: boolean;
    repo_present: boolean;
    initial_sync_complete: boolean;
    last_successful_sync_at: string | null;
    last_successful_commit: string | null;
    last_error: string | null;
    pending_startup_mode: "resume" | "incremental" | null;
  };
}

interface KnowledgeFilesResponse {
  base_id: string;
  files: KnowledgeFile[];
  total_size: number;
  file_count: number;
  file_listing_degraded?: boolean;
  file_listing_error?: string | null;
}

const MIN_CHUNK_SIZE = 128;
const DEFAULT_CHUNK_SIZE = 5000;
const DEFAULT_CHUNK_OVERLAP = 0;

const DEFAULT_BASE_SETTINGS: KnowledgeBaseConfig = {
  path: "./knowledge_docs/default",
  watch: true,
  chunk_size: DEFAULT_CHUNK_SIZE,
  chunk_overlap: DEFAULT_CHUNK_OVERLAP,
};

const DEFAULT_GIT_SETTINGS: KnowledgeGitConfig = {
  repo_url: "",
  branch: "main",
  poll_interval_seconds: 300,
  startup_behavior: "blocking",
  lfs: false,
  sync_timeout_seconds: 3600,
  skip_hidden: true,
};
const REDACTED_CREDENTIALS_SENTINEL = "***";

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatModifiedDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function redactUrlCredentials(value: string): string {
  try {
    const parsed = new URL(value);
    const authority =
      parsed.username || parsed.password
        ? `${REDACTED_CREDENTIALS_SENTINEL}@${parsed.host}`
        : parsed.host;
    return `${parsed.protocol}//${authority}${parsed.pathname}`;
  } catch {
    const withoutQueryOrFragment = value.replace(/[?#].*$/, "");
    return withoutQueryOrFragment.replace(
      /^([a-z][a-z0-9+.-]*:\/\/)([^@\s/?#]+)@/i,
      `$1${REDACTED_CREDENTIALS_SENTINEL}@`,
    );
  }
}

function gitRepoUrlHasRedactionSentinel(value: string): boolean {
  return value.includes(REDACTED_CREDENTIALS_SENTINEL);
}

function formatStartupMode(value: "resume" | "incremental"): string {
  switch (value) {
    case "resume":
      return "Resume";
    case "incremental":
      return "Incremental";
  }
}

function defaultPathForBase(baseName: string): string {
  return `./knowledge_docs/${baseName}`;
}

function sourceTypeForBase(config?: KnowledgeBaseConfig): KnowledgeSourceType {
  return config?.git ? "git" : "local";
}

function defaultGitSettings(
  gitConfig?: KnowledgeGitConfig,
): KnowledgeGitConfig {
  return {
    ...DEFAULT_GIT_SETTINGS,
    ...gitConfig,
  };
}

function normalizeChunking(
  chunkSize: number | undefined,
  chunkOverlap: number | undefined,
): Pick<KnowledgeBaseConfig, "chunk_size" | "chunk_overlap"> {
  const nextChunkSize =
    typeof chunkSize === "number" && Number.isFinite(chunkSize)
      ? Math.max(MIN_CHUNK_SIZE, Math.trunc(chunkSize))
      : DEFAULT_CHUNK_SIZE;
  const requestedOverlap =
    typeof chunkOverlap === "number" && Number.isFinite(chunkOverlap)
      ? Math.max(0, Math.trunc(chunkOverlap))
      : DEFAULT_CHUNK_OVERLAP;
  return {
    chunk_size: nextChunkSize,
    chunk_overlap: Math.min(requestedOverlap, nextChunkSize - 1),
  };
}

function normalizeGitConfig(gitConfig: KnowledgeGitConfig): KnowledgeGitConfig {
  const repoUrl = gitConfig.repo_url.trim();
  return {
    repo_url: repoUrl,
    branch: gitConfig.branch?.trim() || "main",
    poll_interval_seconds:
      typeof gitConfig.poll_interval_seconds === "number" &&
      gitConfig.poll_interval_seconds >= 5
        ? gitConfig.poll_interval_seconds
        : DEFAULT_GIT_SETTINGS.poll_interval_seconds,
    credentials_service: gitConfig.credentials_service?.trim() || undefined,
    startup_behavior:
      gitConfig.startup_behavior === "background" ? "background" : "blocking",
    lfs: gitConfig.lfs ?? DEFAULT_GIT_SETTINGS.lfs,
    sync_timeout_seconds:
      typeof gitConfig.sync_timeout_seconds === "number" &&
      gitConfig.sync_timeout_seconds >= 5
        ? gitConfig.sync_timeout_seconds
        : DEFAULT_GIT_SETTINGS.sync_timeout_seconds,
    skip_hidden: gitConfig.skip_hidden ?? true,
    include_patterns:
      gitConfig.include_patterns && gitConfig.include_patterns.length > 0
        ? gitConfig.include_patterns
        : undefined,
    exclude_patterns:
      gitConfig.exclude_patterns && gitConfig.exclude_patterns.length > 0
        ? gitConfig.exclude_patterns
        : undefined,
  };
}

function parsePatternsFromTextarea(value: string): string[] | undefined {
  const patterns = value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  return patterns.length > 0 ? patterns : undefined;
}

function formatPatternsForTextarea(patterns?: string[]): string {
  return patterns?.join("\n") ?? "";
}

function validateBaseName(baseName: string): string | null {
  if (!baseName.trim()) {
    return "Base name is required";
  }
  if (!/^[a-zA-Z0-9_-]+$/.test(baseName)) {
    return "Base name can only contain letters, numbers, underscores, and hyphens";
  }
  return null;
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = apiErrorMessageFromPayload(payload, detail);
    } catch {
      // Keep fallback detail
    }
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function Knowledge() {
  const {
    config,
    updateKnowledgeBase,
    deleteKnowledgeBase,
    saveConfig,
    isDirty,
  } = useConfigStore();
  const { toast } = useToast();

  const [selectedBase, setSelectedBase] = useState<string>("");
  const [newBaseName, setNewBaseName] = useState("");
  const [newBaseSourceType, setNewBaseSourceType] =
    useState<KnowledgeSourceType>("local");
  const [newBaseGitSettings, setNewBaseGitSettings] =
    useState<KnowledgeGitConfig>(() => defaultGitSettings());
  const [files, setFiles] = useState<KnowledgeFile[]>([]);
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [settings, setSettings] = useState<KnowledgeBaseConfig>(
    DEFAULT_BASE_SETTINGS,
  );
  const [gitRepoUrlReplacement, setGitRepoUrlReplacement] = useState("");
  const [totalSize, setTotalSize] = useState(0);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [reindexing, setReindexing] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [creatingBase, setCreatingBase] = useState(false);
  const [deletingBase, setDeletingBase] = useState(false);
  const [deletingPath, setDeletingPath] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const loadRequestIdRef = useRef(0);

  const knowledgeBases = useMemo(
    () => config?.knowledge_bases ?? {},
    [config?.knowledge_bases],
  );
  const selectedBaseConfig = selectedBase
    ? knowledgeBases[selectedBase]
    : undefined;
  const selectedBaseIsGitBacked = Boolean(selectedBaseConfig?.git);
  const baseNames = useMemo(
    () => Object.keys(knowledgeBases).sort(),
    [knowledgeBases],
  );

  useEffect(() => {
    if (baseNames.length === 0) {
      if (selectedBase !== "") {
        setSelectedBase("");
      }
      return;
    }

    if (!selectedBase || !baseNames.includes(selectedBase)) {
      setSelectedBase(baseNames.length === 1 ? baseNames[0] : "");
    }
  }, [baseNames, selectedBase]);

  useEffect(() => {
    if (!selectedBase) {
      setSettings(DEFAULT_BASE_SETTINGS);
      return;
    }

    const selectedConfig = knowledgeBases[selectedBase];
    if (!selectedConfig) {
      setSettings(DEFAULT_BASE_SETTINGS);
      return;
    }

    const chunking = normalizeChunking(
      selectedConfig.chunk_size,
      selectedConfig.chunk_overlap,
    );
    setSettings({
      path: selectedConfig.path,
      watch: selectedConfig.watch,
      ...chunking,
      git: selectedConfig.git
        ? defaultGitSettings(selectedConfig.git)
        : undefined,
    });
  }, [knowledgeBases, selectedBase]);

  const loadData = useCallback(async (baseId: string | null) => {
    const requestId = loadRequestIdRef.current + 1;
    loadRequestIdRef.current = requestId;
    const isCurrentRequest = () => loadRequestIdRef.current === requestId;

    setLoading(true);
    setError(null);

    try {
      if (!baseId) {
        if (isCurrentRequest()) {
          setFiles([]);
          setStatus(null);
          setTotalSize(0);
        }
        return;
      }

      const [statusData, filesData] = await Promise.all([
        fetchJson<KnowledgeStatus>(API_ENDPOINTS.knowledge.status(baseId)),
        fetchJson<KnowledgeFilesResponse>(
          API_ENDPOINTS.knowledge.files(baseId),
        ),
      ]);

      if (!isCurrentRequest()) {
        return;
      }

      if (statusData.base_id !== baseId || filesData.base_id !== baseId) {
        throw new Error(
          "Loaded knowledge data did not match the selected base",
        );
      }

      if (isCurrentRequest()) {
        setStatus({
          ...statusData,
          file_listing_degraded:
            filesData.file_listing_degraded ?? statusData.file_listing_degraded,
          file_listing_error:
            filesData.file_listing_error ??
            statusData.file_listing_error ??
            null,
        });
        setFiles(filesData.files);
        setTotalSize(filesData.total_size);
      }
    } catch (err) {
      if (!isCurrentRequest()) {
        return;
      }
      const message =
        err instanceof Error ? err.message : "Failed to load knowledge data";
      setError(message);
    } finally {
      if (isCurrentRequest()) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadData(selectedBase || null);
  }, [selectedBase, loadData]);

  const updateSettings = useCallback(
    (updates: Partial<KnowledgeBaseConfig>) => {
      if (!selectedBase) {
        return;
      }

      setSettings((previous) => {
        const next = { ...previous, ...updates };
        updateKnowledgeBase(selectedBase, next);
        return next;
      });
    },
    [selectedBase, updateKnowledgeBase],
  );

  const settingsSourceType: KnowledgeSourceType = settings.git
    ? "git"
    : "local";
  const hasGitRepoUrlReplacement =
    settingsSourceType === "git" && gitRepoUrlReplacement.trim().length > 0;
  const canSaveSettings = isDirty || hasGitRepoUrlReplacement;

  useEffect(() => {
    setGitRepoUrlReplacement("");
  }, [selectedBase, settingsSourceType]);

  const updateGitSettings = useCallback(
    (updates: Partial<KnowledgeGitConfig>) => {
      if (!settings.git) {
        return;
      }
      updateSettings({
        git: {
          ...settings.git,
          ...updates,
        },
      });
    },
    [settings.git, updateSettings],
  );

  const updateNewBaseGitSettings = useCallback(
    (updates: Partial<KnowledgeGitConfig>) => {
      setNewBaseGitSettings((previous) => ({
        ...previous,
        ...updates,
      }));
    },
    [],
  );

  const handleSaveSettings = useCallback(async () => {
    if (!selectedBase) {
      return;
    }

    const repoUrlReplacement = gitRepoUrlReplacement.trim();
    const gitSettings = settings.git
      ? {
          ...settings.git,
          repo_url: repoUrlReplacement || settings.git.repo_url,
        }
      : undefined;

    if (gitSettings && !gitSettings.repo_url.trim()) {
      setError("Repository URL is required when Git source is enabled");
      return;
    }
    if (gitSettings && gitRepoUrlHasRedactionSentinel(gitSettings.repo_url)) {
      setError("Repository URL contains a redacted credential placeholder");
      return;
    }

    const normalizedChunking = normalizeChunking(
      settings.chunk_size,
      settings.chunk_overlap,
    );
    const nextSettings: KnowledgeBaseConfig = gitSettings
      ? {
          ...settings,
          ...normalizedChunking,
          git: normalizeGitConfig(gitSettings),
        }
      : {
          ...settings,
          ...normalizedChunking,
        };

    setSettings(nextSettings);
    updateKnowledgeBase(selectedBase, nextSettings);

    setSavingSettings(true);
    setError(null);
    try {
      const result = await saveConfig();
      if (result.status !== "saved") {
        const message =
          result.status === "error"
            ? result.message
            : "Save was superseded by newer draft edits";
        setError(message);
        toast({
          title: "Save failed",
          description: message,
          variant: "destructive",
        });
        return;
      }
      await loadData(selectedBase);
      setGitRepoUrlReplacement("");
      toast({
        title: "Knowledge settings saved",
        description: "Configuration has been updated.",
      });
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to save knowledge settings";
      setError(message);
      toast({
        title: "Save failed",
        description: message,
        variant: "destructive",
      });
    } finally {
      setSavingSettings(false);
    }
  }, [
    loadData,
    saveConfig,
    selectedBase,
    settings,
    gitRepoUrlReplacement,
    toast,
    updateKnowledgeBase,
  ]);

  const handleCreateBase = useCallback(async () => {
    const baseName = newBaseName.trim();
    const validationError = validateBaseName(baseName);
    if (validationError) {
      setError(validationError);
      return;
    }

    if (baseNames.includes(baseName)) {
      setError(`Knowledge base '${baseName}' already exists`);
      return;
    }

    setCreatingBase(true);
    setError(null);

    try {
      if (newBaseSourceType === "git" && !newBaseGitSettings.repo_url.trim()) {
        setError("Repository URL is required for Git-based knowledge bases");
        return;
      }

      const nextBaseConfig: KnowledgeBaseConfig = {
        path: defaultPathForBase(baseName),
        watch: true,
        chunk_size: DEFAULT_CHUNK_SIZE,
        chunk_overlap: DEFAULT_CHUNK_OVERLAP,
      };

      if (newBaseSourceType === "git") {
        nextBaseConfig.git = normalizeGitConfig(newBaseGitSettings);
      }

      updateKnowledgeBase(baseName, nextBaseConfig);
      const result = await saveConfig();
      if (result.status !== "saved") {
        const message =
          result.status === "error"
            ? result.message
            : "Save was superseded by newer draft edits";
        setError(message);
        toast({
          title: "Create failed",
          description: message,
          variant: "destructive",
        });
        return;
      }
      setSelectedBase(baseName);
      setNewBaseName("");
      setNewBaseSourceType("local");
      setNewBaseGitSettings(defaultGitSettings());
      await loadData(baseName);
      toast({
        title: "Knowledge base created",
        description:
          newBaseSourceType === "git"
            ? `Git base '${baseName}' is ready to sync.`
            : `Base '${baseName}' is ready for uploads.`,
      });
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to create knowledge base";
      setError(message);
      toast({
        title: "Create failed",
        description: message,
        variant: "destructive",
      });
    } finally {
      setCreatingBase(false);
    }
  }, [
    baseNames,
    updateKnowledgeBase,
    loadData,
    newBaseName,
    newBaseSourceType,
    newBaseGitSettings,
    saveConfig,
    toast,
  ]);

  const handleDeleteBase = useCallback(async () => {
    if (!selectedBase) {
      return;
    }

    if (!window.confirm(`Delete knowledge base '${selectedBase}'?`)) {
      return;
    }

    const nextBase =
      baseNames.filter((name) => name !== selectedBase)[0] || null;

    setDeletingBase(true);
    setError(null);

    try {
      deleteKnowledgeBase(selectedBase);
      const result = await saveConfig();
      if (result.status !== "saved") {
        const message =
          result.status === "error"
            ? result.message
            : "Save was superseded by newer draft edits";
        setError(message);
        toast({
          title: "Delete failed",
          description: message,
          variant: "destructive",
        });
        return;
      }
      setSelectedBase(nextBase || "");
      await loadData(nextBase);
      toast({
        title: "Knowledge base deleted",
        description: `Deleted '${selectedBase}'.`,
      });
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to delete knowledge base";
      setError(message);
      toast({
        title: "Delete failed",
        description: message,
        variant: "destructive",
      });
    } finally {
      setDeletingBase(false);
    }
  }, [
    baseNames,
    deleteKnowledgeBase,
    loadData,
    saveConfig,
    selectedBase,
    toast,
  ]);

  const uploadFiles = useCallback(
    async (selectedFiles: File[]) => {
      if (selectedFiles.length === 0 || !selectedBase) {
        return;
      }
      if (selectedBaseIsGitBacked) {
        setError(
          "Git-backed knowledge bases sync from the repository. Update the repository and reindex instead.",
        );
        return;
      }

      const formData = new FormData();
      selectedFiles.forEach((file) => {
        formData.append("files", file);
      });

      setUploading(true);
      setError(null);

      try {
        await fetchJson<{ uploaded: string[] }>(
          API_ENDPOINTS.knowledge.upload(selectedBase),
          {
            method: "POST",
            body: formData,
          },
        );
        await loadData(selectedBase);
        toast({
          title: "Upload complete",
          description: `Uploaded ${selectedFiles.length} file${
            selectedFiles.length === 1 ? "" : "s"
          } to '${selectedBase}'.`,
        });
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Failed to upload files";
        setError(message);
        toast({
          title: "Upload failed",
          description: message,
          variant: "destructive",
        });
      } finally {
        setUploading(false);
      }
    },
    [loadData, selectedBase, selectedBaseIsGitBacked, toast],
  );

  const handleDeleteFile = useCallback(
    async (path: string) => {
      if (!selectedBase) {
        return;
      }
      if (selectedBaseIsGitBacked) {
        setError(
          "Git-backed knowledge bases sync from the repository. Update the repository and reindex instead.",
        );
        return;
      }

      if (
        !window.confirm(
          `Delete '${path}' from knowledge base '${selectedBase}'?`,
        )
      ) {
        return;
      }

      setDeletingPath(path);
      setError(null);

      try {
        await fetchJson<{ success: boolean }>(
          API_ENDPOINTS.knowledge.deleteFile(selectedBase, path),
          {
            method: "DELETE",
          },
        );
        await loadData(selectedBase);
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Failed to delete file";
        setError(message);
        toast({
          title: "Delete failed",
          description: message,
          variant: "destructive",
        });
      } finally {
        setDeletingPath(null);
      }
    },
    [loadData, selectedBase, selectedBaseIsGitBacked, toast],
  );

  const handleReindex = useCallback(async () => {
    if (!selectedBase) {
      return;
    }

    setReindexing(true);
    setError(null);

    try {
      await fetchJson<{ indexed_count: number }>(
        API_ENDPOINTS.knowledge.reindex(selectedBase),
        {
          method: "POST",
        },
      );
      await loadData(selectedBase);
      toast({
        title: "Reindex complete",
        description: `Knowledge base '${selectedBase}' rebuilt successfully.`,
      });
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to reindex knowledge files";
      setError(message);
      toast({
        title: "Reindex failed",
        description: message,
        variant: "destructive",
      });
    } finally {
      setReindexing(false);
    }
  }, [loadData, selectedBase, toast]);

  const columns: ColumnDef<KnowledgeFile>[] = useMemo(() => {
    const baseColumns: ColumnDef<KnowledgeFile>[] = [
      {
        accessorKey: "name",
        header: () => <span className="font-medium">Name</span>,
        cell: ({ row }) => (
          <div className="space-y-1">
            <div className="font-medium">{row.original.name}</div>
            <code className="text-xs text-muted-foreground">
              {row.original.path}
            </code>
          </div>
        ),
      },
      {
        accessorKey: "size",
        header: () => <span className="font-medium">Size</span>,
        cell: ({ row }) => formatBytes(row.original.size),
      },
      {
        accessorKey: "type",
        header: () => <span className="font-medium">Type</span>,
        cell: ({ row }) => <Badge variant="outline">{row.original.type}</Badge>,
      },
      {
        accessorKey: "modified",
        header: () => <span className="font-medium">Modified</span>,
        cell: ({ row }) => formatModifiedDate(row.original.modified),
      },
    ];

    if (selectedBaseIsGitBacked) {
      return baseColumns;
    }

    return [
      ...baseColumns,
      {
        id: "actions",
        header: () => <span className="font-medium">Actions</span>,
        cell: ({ row }) => (
          <div className="flex justify-end">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => handleDeleteFile(row.original.path)}
              disabled={deletingPath === row.original.path || isDirty}
              title="Delete file"
              aria-label={`Delete ${row.original.path}`}
            >
              <Trash2 className="h-4 w-4 text-destructive" />
            </Button>
          </div>
        ),
      },
    ];
  }, [deletingPath, handleDeleteFile, isDirty, selectedBaseIsGitBacked]);

  const table = useReactTable({
    data: files,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  const onFileInputChange = (event: ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = event.target.files
      ? Array.from(event.target.files)
      : [];
    void uploadFiles(selectedFiles);
    event.target.value = "";
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    const droppedFiles = Array.from(event.dataTransfer.files || []);
    void uploadFiles(droppedFiles);
  };

  const createBaseNamePreview = newBaseName.trim() || "new_base_name";
  const createBasePathPreview = defaultPathForBase(createBaseNamePreview);

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="h-24 rounded-lg bg-muted animate-pulse" />
        <div className="h-96 rounded-lg bg-muted animate-pulse" />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto overflow-x-hidden">
      <div className="h-full flex flex-col gap-4">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-xl">Knowledge Bases</CardTitle>
            <CardDescription>
              Manage separate knowledge bases and assign agents to a specific
              base.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">
                Configured Bases: {baseNames.length}
              </Badge>
              {selectedBase && (
                <Badge variant="default">Active: {selectedBase}</Badge>
              )}
            </div>

            <div className="space-y-3">
              {baseNames.length > 0 ? (
                <>
                  <p className="text-sm text-muted-foreground">
                    Select a base to manage. Settings and files below always
                    belong to the active base.
                  </p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2">
                    {baseNames.map((baseName) => {
                      const baseConfig = knowledgeBases[baseName];
                      const isActive = baseName === selectedBase;
                      const baseSourceType = sourceTypeForBase(baseConfig);
                      return (
                        <button
                          key={baseName}
                          type="button"
                          onClick={() => setSelectedBase(baseName)}
                          className={cn(
                            "rounded-md border p-3 text-left transition-colors",
                            isActive
                              ? "border-primary bg-primary/5"
                              : "border-border hover:border-primary/40 hover:bg-muted/40",
                          )}
                          aria-pressed={isActive}
                        >
                          <div className="flex items-center justify-between gap-2">
                            <div className="flex items-center gap-2">
                              <span className="font-medium">{baseName}</span>
                              {baseSourceType === "git" ? (
                                <Badge variant="secondary" className="gap-1">
                                  <GitBranch className="h-3 w-3" />
                                  Git
                                </Badge>
                              ) : (
                                <Badge variant="outline">Local</Badge>
                              )}
                            </div>
                            {isActive && (
                              <Badge variant="default">Active</Badge>
                            )}
                          </div>
                          {baseSourceType === "git" ? (
                            <>
                              <p className="mt-1 truncate text-xs font-mono text-muted-foreground">
                                {baseConfig?.git?.repo_url
                                  ? redactUrlCredentials(
                                      baseConfig.git.repo_url,
                                    )
                                  : "Repository URL not configured"}
                              </p>
                              <p className="mt-1 text-xs text-muted-foreground">
                                Branch: {baseConfig?.git?.branch || "main"}
                              </p>
                            </>
                          ) : (
                            <>
                              <p className="mt-1 truncate text-xs font-mono text-muted-foreground">
                                {baseConfig?.path ??
                                  defaultPathForBase(baseName)}
                              </p>
                              <p className="mt-1 text-xs text-muted-foreground">
                                {baseConfig?.watch
                                  ? "Advisory refresh on access"
                                  : "External edits need reindex"}
                              </p>
                            </>
                          )}
                        </button>
                      );
                    })}
                  </div>
                </>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No knowledge bases configured yet. Add one below to start.
                </p>
              )}

              <Button
                variant="outline"
                onClick={handleDeleteBase}
                disabled={!selectedBase || deletingBase || isDirty}
                className="w-full sm:w-auto"
              >
                <Trash2 className="h-4 w-4 mr-2" />
                {deletingBase ? "Deleting..." : "Delete Active Base"}
              </Button>
            </div>

            <div className="space-y-4 rounded-md border p-4">
              <div className="space-y-1">
                <p className="text-sm font-medium">Create Knowledge Base</p>
                <p className="text-xs text-muted-foreground">
                  Choose a source type first. Git bases can be configured in one
                  step.
                </p>
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium" htmlFor="new-base-name">
                  Base Name
                </label>
                <Input
                  id="new-base-name"
                  value={newBaseName}
                  onChange={(event) => setNewBaseName(event.target.value)}
                  placeholder="new_base_name"
                />
              </div>

              <div className="space-y-2">
                <p className="text-sm font-medium">Source Type</p>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  <Button
                    type="button"
                    variant={
                      newBaseSourceType === "local" ? "default" : "outline"
                    }
                    aria-label="Create local source"
                    onClick={() => setNewBaseSourceType("local")}
                    disabled={creatingBase || isDirty}
                    className="justify-start"
                  >
                    <FolderOpen className="h-4 w-4 mr-2" />
                    Local Folder
                  </Button>
                  <Button
                    type="button"
                    variant={
                      newBaseSourceType === "git" ? "default" : "outline"
                    }
                    aria-label="Create git source"
                    onClick={() => setNewBaseSourceType("git")}
                    disabled={creatingBase || isDirty}
                    className="justify-start"
                  >
                    <GitBranch className="h-4 w-4 mr-2" />
                    Git Repository
                  </Button>
                </div>
              </div>

              {newBaseSourceType === "git" ? (
                <div className="space-y-3 rounded-md border p-3">
                  <div className="space-y-2">
                    <label
                      className="text-sm font-medium"
                      htmlFor="new-base-git-repo-url"
                    >
                      Repository URL
                    </label>
                    <Input
                      id="new-base-git-repo-url"
                      value={newBaseGitSettings.repo_url}
                      onChange={(event) =>
                        updateNewBaseGitSettings({
                          repo_url: event.target.value,
                        })
                      }
                      placeholder="https://github.com/org/repo"
                    />
                  </div>
                  <div className="space-y-2">
                    <label
                      className="text-sm font-medium"
                      htmlFor="new-base-git-branch"
                    >
                      Branch
                    </label>
                    <Input
                      id="new-base-git-branch"
                      value={newBaseGitSettings.branch ?? "main"}
                      onChange={(event) =>
                        updateNewBaseGitSettings({
                          branch: event.target.value || "main",
                        })
                      }
                      placeholder="main"
                    />
                  </div>
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Local base folder path: <code>{createBasePathPreview}</code>
                </p>
              )}

              <div className="flex justify-end">
                <Button
                  variant="outline"
                  onClick={handleCreateBase}
                  disabled={creatingBase || isDirty}
                >
                  <Plus className="h-4 w-4 mr-2" />
                  {creatingBase
                    ? "Creating..."
                    : newBaseSourceType === "git"
                      ? "Create Git Base"
                      : "Add Base"}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>

        {selectedBase ? (
          <>
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Base Settings</CardTitle>
                <CardDescription>
                  Configure source and sync behavior for{" "}
                  <code>{selectedBase}</code>.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <p className="text-sm font-medium">Source Type</p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    <Button
                      type="button"
                      variant={
                        settingsSourceType === "local" ? "default" : "outline"
                      }
                      aria-label="Settings local source"
                      onClick={() => updateSettings({ git: undefined })}
                      disabled={savingSettings}
                      className="justify-start"
                    >
                      <FolderOpen className="h-4 w-4 mr-2" />
                      Local Folder
                    </Button>
                    <Button
                      type="button"
                      variant={
                        settingsSourceType === "git" ? "default" : "outline"
                      }
                      aria-label="Settings git source"
                      onClick={() =>
                        updateSettings({
                          git: defaultGitSettings(settings.git),
                        })
                      }
                      disabled={savingSettings}
                      className="justify-start"
                    >
                      <GitBranch className="h-4 w-4 mr-2" />
                      Git Repository
                    </Button>
                  </div>
                </div>

                <div className="space-y-2">
                  <label
                    className="text-sm font-medium"
                    htmlFor="knowledge-path"
                  >
                    Folder Path
                  </label>
                  <Input
                    id="knowledge-path"
                    value={settings.path}
                    onChange={(event) =>
                      updateSettings({ path: event.target.value })
                    }
                    placeholder={defaultPathForBase(selectedBase)}
                  />
                </div>

                <div className="flex items-center justify-between rounded-md border p-3">
                  <div className="space-y-1">
                    <p className="text-sm font-medium">Refresh on Access</p>
                    <p className="text-xs text-muted-foreground">
                      Schedule a background refresh when assigned agents access
                      this base.
                    </p>
                  </div>
                  <Checkbox
                    checked={settings.watch}
                    onCheckedChange={(checked) =>
                      updateSettings({ watch: checked === true })
                    }
                  />
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="space-y-2">
                    <label
                      className="text-sm font-medium"
                      htmlFor="knowledge-chunk-size"
                    >
                      Chunk Size (characters)
                    </label>
                    <Input
                      id="knowledge-chunk-size"
                      type="number"
                      min={MIN_CHUNK_SIZE}
                      value={settings.chunk_size ?? DEFAULT_CHUNK_SIZE}
                      onChange={(event) => {
                        const parsedValue = Number.parseInt(
                          event.target.value,
                          10,
                        );
                        updateSettings({
                          chunk_size:
                            Number.isNaN(parsedValue) ||
                            parsedValue < MIN_CHUNK_SIZE
                              ? MIN_CHUNK_SIZE
                              : parsedValue,
                        });
                      }}
                    />
                    <p className="text-xs text-muted-foreground">
                      Larger chunks reduce requests but increase per-request
                      token load.
                    </p>
                  </div>

                  <div className="space-y-2">
                    <label
                      className="text-sm font-medium"
                      htmlFor="knowledge-chunk-overlap"
                    >
                      Chunk Overlap (characters)
                    </label>
                    <Input
                      id="knowledge-chunk-overlap"
                      type="number"
                      min={0}
                      value={settings.chunk_overlap ?? DEFAULT_CHUNK_OVERLAP}
                      onChange={(event) => {
                        const parsedValue = Number.parseInt(
                          event.target.value,
                          10,
                        );
                        const nextChunkSize =
                          settings.chunk_size ?? DEFAULT_CHUNK_SIZE;
                        const normalizedValue = Number.isNaN(parsedValue)
                          ? 0
                          : Math.max(
                              0,
                              Math.min(parsedValue, nextChunkSize - 1),
                            );
                        updateSettings({
                          chunk_overlap: normalizedValue,
                        });
                      }}
                    />
                    <p className="text-xs text-muted-foreground">
                      Must stay lower than chunk size.
                    </p>
                  </div>
                </div>

                {settingsSourceType === "git" && settings.git ? (
                  <div className="space-y-4 rounded-md border p-3">
                    <div className="space-y-2">
                      <label
                        className="text-sm font-medium"
                        htmlFor="knowledge-git-current-repo-url"
                      >
                        Current Repository URL
                      </label>
                      <Input
                        id="knowledge-git-current-repo-url"
                        value={redactUrlCredentials(settings.git.repo_url)}
                        readOnly
                        placeholder="https://github.com/org/repo"
                      />
                    </div>

                    <div className="space-y-2">
                      <label
                        className="text-sm font-medium"
                        htmlFor="knowledge-git-replacement-repo-url"
                      >
                        Replacement Repository URL
                      </label>
                      <Input
                        id="knowledge-git-replacement-repo-url"
                        value={gitRepoUrlReplacement}
                        onChange={(event) =>
                          setGitRepoUrlReplacement(event.target.value)
                        }
                        placeholder="https://github.com/org/repo"
                      />
                    </div>

                    <div className="space-y-2">
                      <label
                        className="text-sm font-medium"
                        htmlFor="knowledge-git-branch"
                      >
                        Branch
                      </label>
                      <Input
                        id="knowledge-git-branch"
                        value={settings.git.branch ?? "main"}
                        onChange={(event) =>
                          updateGitSettings({
                            branch: event.target.value || "main",
                          })
                        }
                        placeholder="main"
                      />
                    </div>

                    <div className="space-y-2">
                      <label
                        className="text-sm font-medium"
                        htmlFor="knowledge-git-poll-interval-seconds"
                      >
                        Poll Interval (seconds)
                      </label>
                      <Input
                        id="knowledge-git-poll-interval-seconds"
                        type="number"
                        min={5}
                        value={settings.git.poll_interval_seconds ?? 300}
                        onChange={(event) => {
                          const nextValue = Number.parseInt(
                            event.target.value,
                            10,
                          );
                          updateGitSettings({
                            poll_interval_seconds:
                              Number.isNaN(nextValue) || nextValue < 5
                                ? 5
                                : nextValue,
                          });
                        }}
                      />
                      <p className="text-xs text-muted-foreground">
                        Check for updates every X seconds.
                      </p>
                    </div>

                    <div className="space-y-2">
                      <label
                        className="text-sm font-medium"
                        htmlFor="knowledge-git-startup-behavior"
                      >
                        Legacy Startup Behavior
                      </label>
                      <Select
                        value={settings.git.startup_behavior ?? "blocking"}
                        onValueChange={(value) =>
                          updateGitSettings({
                            startup_behavior:
                              value === "background"
                                ? "background"
                                : "blocking",
                          })
                        }
                      >
                        <SelectTrigger id="knowledge-git-startup-behavior">
                          <SelectValue placeholder="Select startup behavior" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="blocking">Blocking</SelectItem>
                          <SelectItem value="background">Background</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        Retained for compatibility. Git refreshes are scheduled
                        on access or by explicit reindex.
                      </p>
                    </div>

                    <div className="space-y-2">
                      <label
                        className="text-sm font-medium"
                        htmlFor="knowledge-git-credentials-service"
                      >
                        Credentials Service (optional)
                      </label>
                      <Input
                        id="knowledge-git-credentials-service"
                        value={settings.git.credentials_service ?? ""}
                        onChange={(event) =>
                          updateGitSettings({
                            credentials_service:
                              event.target.value || undefined,
                          })
                        }
                        placeholder="github-pat"
                      />
                      <p className="text-xs text-muted-foreground">
                        Service name in Credentials tab for private HTTPS repos.
                      </p>
                    </div>

                    <div className="space-y-2">
                      <label
                        className="text-sm font-medium"
                        htmlFor="knowledge-git-sync-timeout-seconds"
                      >
                        Sync Timeout (seconds)
                      </label>
                      <Input
                        id="knowledge-git-sync-timeout-seconds"
                        type="number"
                        min={5}
                        value={settings.git.sync_timeout_seconds ?? 3600}
                        onChange={(event) => {
                          const nextValue = Number.parseInt(
                            event.target.value,
                            10,
                          );
                          updateGitSettings({
                            sync_timeout_seconds:
                              Number.isNaN(nextValue) || nextValue < 5
                                ? 5
                                : nextValue,
                          });
                        }}
                      />
                      <p className="text-xs text-muted-foreground">
                        Abort a hung Git command after this many seconds.
                      </p>
                    </div>

                    <div className="flex items-center justify-between rounded-md border p-3">
                      <div className="space-y-1">
                        <p className="text-sm font-medium">Enable Git LFS</p>
                        <p className="text-xs text-muted-foreground">
                          Pull Git LFS objects after each sync for repositories
                          with large files.
                        </p>
                      </div>
                      <Checkbox
                        aria-label="Enable Git LFS"
                        checked={settings.git.lfs ?? false}
                        onCheckedChange={(checked) =>
                          updateGitSettings({ lfs: checked === true })
                        }
                      />
                    </div>

                    <div className="flex items-center justify-between rounded-md border p-3">
                      <div className="space-y-1">
                        <p className="text-sm font-medium">Skip Hidden Files</p>
                        <p className="text-xs text-muted-foreground">
                          Ignore dotfiles and hidden paths while indexing.
                        </p>
                      </div>
                      <Checkbox
                        aria-label="Skip Hidden Files"
                        checked={settings.git.skip_hidden ?? true}
                        onCheckedChange={(checked) =>
                          updateGitSettings({ skip_hidden: checked === true })
                        }
                      />
                    </div>

                    <div className="space-y-2">
                      <label
                        className="text-sm font-medium"
                        htmlFor="knowledge-git-include-patterns"
                      >
                        Include Patterns (optional)
                      </label>
                      <Textarea
                        id="knowledge-git-include-patterns"
                        value={formatPatternsForTextarea(
                          settings.git.include_patterns,
                        )}
                        onChange={(event) =>
                          updateGitSettings({
                            include_patterns: parsePatternsFromTextarea(
                              event.target.value,
                            ),
                          })
                        }
                        placeholder="docs/**"
                        className="min-h-[96px]"
                      />
                      <p className="text-xs text-muted-foreground">
                        Root-anchored glob patterns. Only matching files will be
                        indexed.
                      </p>
                    </div>

                    <div className="space-y-2">
                      <label
                        className="text-sm font-medium"
                        htmlFor="knowledge-git-exclude-patterns"
                      >
                        Exclude Patterns (optional)
                      </label>
                      <Textarea
                        id="knowledge-git-exclude-patterns"
                        value={formatPatternsForTextarea(
                          settings.git.exclude_patterns,
                        )}
                        onChange={(event) =>
                          updateGitSettings({
                            exclude_patterns: parsePatternsFromTextarea(
                              event.target.value,
                            ),
                          })
                        }
                        placeholder="docs/private/**"
                        className="min-h-[96px]"
                      />
                      <p className="text-xs text-muted-foreground">
                        Root-anchored glob patterns to exclude after include
                        filtering.
                      </p>
                    </div>

                    <p className="text-xs text-muted-foreground">
                      File upload and delete actions are disabled for Git
                      sources. Update the repository and reindex to refresh
                      content.
                    </p>
                  </div>
                ) : null}

                <div className="flex justify-end">
                  <Button
                    variant="outline"
                    onClick={handleSaveSettings}
                    disabled={savingSettings || !canSaveSettings}
                  >
                    {savingSettings ? "Saving..." : "Save Settings"}
                  </Button>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardContent className="py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline">
                    Files: {status?.file_count ?? 0}
                  </Badge>
                  <Badge variant="outline">
                    Indexed: {status?.indexed_count ?? 0}
                  </Badge>
                  <Badge variant="outline">
                    Total Size: {formatBytes(totalSize)}
                  </Badge>
                  {status?.refreshing ||
                  status?.refresh_job === "running" ||
                  status?.advisory_state === "refreshing" ? (
                    <Badge variant="default">Refresh Running</Badge>
                  ) : null}
                  {status?.refresh_job === "pending" ||
                  status?.advisory_state === "stale" ? (
                    <Badge variant="secondary">Refresh Pending</Badge>
                  ) : null}
                  {status?.refresh_job === "failed" ||
                  status?.advisory_state === "refresh_failed" ||
                  status?.last_error ? (
                    <Badge variant="destructive">Refresh Failed</Badge>
                  ) : null}
                  {status?.file_listing_degraded ? (
                    <Badge variant="destructive">File Listing Degraded</Badge>
                  ) : null}
                  {status?.git ? (
                    <>
                      <Badge variant="outline">
                        Git policy: {status.git.startup_behavior}
                      </Badge>
                      <Badge
                        variant={status.git.syncing ? "default" : "outline"}
                      >
                        {status.git.syncing ? "Refreshing" : "Idle"}
                      </Badge>
                      <Badge variant="outline">
                        {status.git.repo_present
                          ? "Repo Present"
                          : "Repo Missing"}
                      </Badge>
                      <Badge variant="outline">
                        {status.git.initial_sync_complete
                          ? "Snapshot Ready"
                          : "Snapshot Pending"}
                      </Badge>
                      {status.git.pending_startup_mode ? (
                        <Badge variant="secondary">
                          Pending refresh:{" "}
                          {formatStartupMode(status.git.pending_startup_mode)}
                        </Badge>
                      ) : null}
                      {status.git.lfs ? (
                        <Badge variant="secondary">LFS</Badge>
                      ) : null}
                      {status.git.last_error ? (
                        <Badge variant="destructive">Git Error</Badge>
                      ) : null}
                    </>
                  ) : null}
                </div>
                <p className="text-sm text-muted-foreground mt-2">
                  Folder: <code>{status?.folder_path ?? "-"}</code>
                </p>
                {status?.last_error ? (
                  <p className="mt-2 text-sm text-destructive">
                    Refresh Error: {status.last_error}
                  </p>
                ) : null}
                {status?.file_listing_degraded ? (
                  <p className="mt-2 text-sm text-destructive">
                    File Listing:{" "}
                    {status.file_listing_error ??
                      "Knowledge file listing is incomplete."}
                  </p>
                ) : null}
                {status?.git ? (
                  <div className="mt-2 space-y-1 text-sm text-muted-foreground">
                    <p>
                      Repo:{" "}
                      <code>{redactUrlCredentials(status.git.repo_url)}</code> (
                      {status.git.branch})
                    </p>
                    {status.git.last_successful_commit ? (
                      <p>
                        Last Commit:{" "}
                        <code>{status.git.last_successful_commit}</code>
                      </p>
                    ) : null}
                    {status.git.last_successful_sync_at ? (
                      <p>
                        Last Sync:{" "}
                        {formatModifiedDate(status.git.last_successful_sync_at)}
                      </p>
                    ) : null}
                    {status.git.last_error ? (
                      <p>Git Error: {status.git.last_error}</p>
                    ) : null}
                  </div>
                ) : null}
              </CardContent>
            </Card>
          </>
        ) : null}

        {isDirty && (
          <Card className="border-amber-500/30">
            <CardContent className="py-3 text-sm text-amber-700 dark:text-amber-300">
              Save settings before uploading, deleting, or reindexing files.
            </CardContent>
          </Card>
        )}

        {error && (
          <Card className="border-destructive/30">
            <CardContent className="py-3 text-sm text-destructive">
              {error}
            </CardContent>
          </Card>
        )}

        {selectedBase && (
          <>
            {selectedBaseIsGitBacked ? (
              <Card>
                <CardContent className="py-6">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <p className="font-medium">Repository-managed files</p>
                      <p className="text-sm text-muted-foreground">
                        Update the configured repository, then reindex to sync
                        and rebuild this base.
                      </p>
                    </div>
                    <Button
                      variant="outline"
                      onClick={handleReindex}
                      disabled={reindexing || isDirty}
                    >
                      <RefreshCw
                        className={cn(
                          "h-4 w-4 mr-2",
                          reindexing && "animate-spin",
                        )}
                      />
                      {reindexing ? "Reindexing..." : "Reindex"}
                    </Button>
                  </div>
                </CardContent>
              </Card>
            ) : (
              <Card
                className={cn(
                  "border-dashed transition-colors",
                  dragActive ? "border-primary bg-primary/5" : "border-border",
                )}
                onDragOver={(event) => {
                  event.preventDefault();
                  setDragActive(true);
                }}
                onDragLeave={(event) => {
                  event.preventDefault();
                  setDragActive(false);
                }}
                onDrop={onDrop}
              >
                <CardContent className="py-6">
                  <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                    <div>
                      <p className="font-medium">
                        Drop files here or upload manually
                      </p>
                      <p className="text-sm text-muted-foreground">
                        Supported formats are auto-detected by agno readers.
                      </p>
                    </div>
                    <div className="flex gap-2">
                      <input
                        ref={fileInputRef}
                        type="file"
                        className="hidden"
                        multiple
                        onChange={onFileInputChange}
                      />
                      <Button
                        variant="outline"
                        onClick={() => fileInputRef.current?.click()}
                        disabled={uploading || isDirty}
                      >
                        <Upload className="h-4 w-4 mr-2" />
                        {uploading ? "Uploading..." : "Upload"}
                      </Button>
                      <Button
                        variant="outline"
                        onClick={handleReindex}
                        disabled={reindexing || isDirty}
                      >
                        <RefreshCw
                          className={cn(
                            "h-4 w-4 mr-2",
                            reindexing && "animate-spin",
                          )}
                        />
                        {reindexing ? "Reindexing..." : "Reindex"}
                      </Button>
                    </div>
                  </div>
                </CardContent>
              </Card>
            )}

            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Knowledge Files</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="max-h-[55vh] overflow-auto rounded-md border">
                  <table className="w-full min-w-[760px] text-sm">
                    <thead>
                      {table.getHeaderGroups().map((headerGroup) => (
                        <tr
                          key={headerGroup.id}
                          className="border-b bg-muted/50"
                        >
                          {headerGroup.headers.map((header) => (
                            <th
                              key={header.id}
                              className="px-4 py-2.5 text-left align-middle"
                            >
                              {header.isPlaceholder
                                ? null
                                : flexRender(
                                    header.column.columnDef.header,
                                    header.getContext(),
                                  )}
                            </th>
                          ))}
                        </tr>
                      ))}
                    </thead>
                    <tbody>
                      {table.getRowModel().rows.length === 0 ? (
                        <tr>
                          <td
                            colSpan={columns.length}
                            className="px-4 py-8 text-center text-muted-foreground"
                          >
                            {selectedBaseIsGitBacked
                              ? "No repository files available yet."
                              : "No files uploaded yet."}
                          </td>
                        </tr>
                      ) : (
                        table.getRowModel().rows.map((row) => (
                          <tr key={row.id} className="border-b last:border-b-0">
                            {row.getVisibleCells().map((cell) => (
                              <td
                                key={cell.id}
                                className="px-4 py-2.5 align-middle"
                              >
                                {flexRender(
                                  cell.column.columnDef.cell,
                                  cell.getContext(),
                                )}
                              </td>
                            ))}
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </CardContent>
            </Card>
          </>
        )}
        {!selectedBase && baseNames.length > 0 && (
          <Card>
            <CardContent className="py-4 text-sm text-muted-foreground">
              Select a knowledge base to view and manage files.
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
