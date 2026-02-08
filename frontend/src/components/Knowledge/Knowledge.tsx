import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from 'react';
import { type ColumnDef, flexRender, getCoreRowModel, useReactTable } from '@tanstack/react-table';
import { API_ENDPOINTS } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useConfigStore } from '@/store/configStore';
import type { KnowledgeConfig as KnowledgeSettings } from '@/types/config';
import { useToast } from '@/components/ui/use-toast';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { RefreshCw, Trash2, Upload } from 'lucide-react';

interface KnowledgeFile {
  name: string;
  path: string;
  size: number;
  modified: string;
  type: string;
}

interface KnowledgeStatus {
  enabled: boolean;
  folder_path: string;
  file_count: number;
  indexed_count: number;
}

interface KnowledgeFilesResponse {
  files: KnowledgeFile[];
  total_size: number;
  file_count: number;
}

const DEFAULT_SETTINGS: KnowledgeSettings = {
  enabled: false,
  path: './knowledge_docs',
  watch: true,
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatModifiedDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      if (typeof payload?.detail === 'string') {
        detail = payload.detail;
      }
    } catch {
      // Keep fallback detail
    }
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function Knowledge() {
  const { config, updateKnowledgeConfig, saveConfig, isDirty } = useConfigStore();
  const { toast } = useToast();

  const [files, setFiles] = useState<KnowledgeFile[]>([]);
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [settings, setSettings] = useState<KnowledgeSettings>(DEFAULT_SETTINGS);
  const [totalSize, setTotalSize] = useState(0);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [reindexing, setReindexing] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [deletingPath, setDeletingPath] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const [statusData, filesData] = await Promise.all([
        fetchJson<KnowledgeStatus>(API_ENDPOINTS.knowledge.status),
        fetchJson<KnowledgeFilesResponse>(API_ENDPOINTS.knowledge.files),
      ]);

      setStatus(statusData);
      setFiles(filesData.files);
      setTotalSize(filesData.total_size);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load knowledge data';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  useEffect(() => {
    const knowledgeConfig = config?.knowledge;
    if (!knowledgeConfig) {
      setSettings(DEFAULT_SETTINGS);
      return;
    }
    setSettings({
      enabled: knowledgeConfig.enabled,
      path: knowledgeConfig.path,
      watch: knowledgeConfig.watch,
    });
  }, [config]);

  const updateSettings = useCallback(
    (updates: Partial<KnowledgeSettings>) => {
      setSettings(previous => {
        const next = { ...previous, ...updates };
        updateKnowledgeConfig(next);
        return next;
      });
    },
    [updateKnowledgeConfig]
  );

  const handleSaveSettings = useCallback(async () => {
    setSavingSettings(true);
    setError(null);
    try {
      await saveConfig();
      await loadData();
      toast({
        title: 'Knowledge settings saved',
        description: 'Configuration has been updated.',
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to save knowledge settings';
      setError(message);
      toast({
        title: 'Save failed',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setSavingSettings(false);
    }
  }, [loadData, saveConfig, toast]);

  const uploadFiles = useCallback(
    async (selectedFiles: File[]) => {
      if (selectedFiles.length === 0) {
        return;
      }

      const formData = new FormData();
      selectedFiles.forEach(file => {
        formData.append('files', file);
      });

      setUploading(true);
      setError(null);

      try {
        await fetchJson<{ uploaded: string[] }>(API_ENDPOINTS.knowledge.upload, {
          method: 'POST',
          body: formData,
        });
        await loadData();
        toast({
          title: 'Upload complete',
          description: `Uploaded ${selectedFiles.length} file${
            selectedFiles.length === 1 ? '' : 's'
          }.`,
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Failed to upload files';
        setError(message);
        toast({
          title: 'Upload failed',
          description: message,
          variant: 'destructive',
        });
      } finally {
        setUploading(false);
      }
    },
    [loadData, toast]
  );

  const handleDelete = useCallback(
    async (path: string) => {
      if (!window.confirm(`Delete '${path}' from the knowledge folder?`)) {
        return;
      }

      setDeletingPath(path);
      setError(null);

      try {
        await fetchJson<{ success: boolean }>(API_ENDPOINTS.knowledge.deleteFile(path), {
          method: 'DELETE',
        });
        await loadData();
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Failed to delete file';
        setError(message);
        toast({
          title: 'Delete failed',
          description: message,
          variant: 'destructive',
        });
      } finally {
        setDeletingPath(null);
      }
    },
    [loadData, toast]
  );

  const handleReindex = useCallback(async () => {
    setReindexing(true);
    setError(null);

    try {
      await fetchJson<{ indexed_count: number }>(API_ENDPOINTS.knowledge.reindex, {
        method: 'POST',
      });
      await loadData();
      toast({
        title: 'Reindex complete',
        description: 'Knowledge index rebuilt successfully.',
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to reindex knowledge files';
      setError(message);
      toast({
        title: 'Reindex failed',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setReindexing(false);
    }
  }, [loadData, toast]);

  const columns: ColumnDef<KnowledgeFile>[] = useMemo(
    () => [
      {
        accessorKey: 'name',
        header: () => <span className="font-medium">Name</span>,
        cell: ({ row }) => (
          <div className="space-y-1">
            <div className="font-medium">{row.original.name}</div>
            <code className="text-xs text-muted-foreground">{row.original.path}</code>
          </div>
        ),
      },
      {
        accessorKey: 'size',
        header: () => <span className="font-medium">Size</span>,
        cell: ({ row }) => formatBytes(row.original.size),
      },
      {
        accessorKey: 'type',
        header: () => <span className="font-medium">Type</span>,
        cell: ({ row }) => <Badge variant="outline">{row.original.type}</Badge>,
      },
      {
        accessorKey: 'modified',
        header: () => <span className="font-medium">Modified</span>,
        cell: ({ row }) => formatModifiedDate(row.original.modified),
      },
      {
        id: 'actions',
        header: () => <span className="font-medium">Actions</span>,
        cell: ({ row }) => (
          <div className="flex justify-end">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => handleDelete(row.original.path)}
              disabled={deletingPath === row.original.path}
              title="Delete file"
            >
              <Trash2 className="h-4 w-4 text-destructive" />
            </Button>
          </div>
        ),
      },
    ],
    [deletingPath, handleDelete]
  );

  const table = useReactTable({
    data: files,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  const onFileInputChange = (event: ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = event.target.files ? Array.from(event.target.files) : [];
    void uploadFiles(selectedFiles);
    event.target.value = '';
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    const droppedFiles = Array.from(event.dataTransfer.files || []);
    void uploadFiles(droppedFiles);
  };

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="h-24 rounded-lg bg-muted animate-pulse" />
        <div className="h-96 rounded-lg bg-muted animate-pulse" />
      </div>
    );
  }

  const enabled = status?.enabled ?? false;

  return (
    <div className="h-full overflow-hidden">
      <div className="h-full flex flex-col gap-4">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-xl">Knowledge Base</CardTitle>
            <CardDescription>
              Upload files to make them searchable by knowledge-enabled agents.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={enabled ? 'default' : 'secondary'}>
                {enabled ? 'Enabled' : 'Disabled'}
              </Badge>
              <Badge variant="outline">Files: {status?.file_count ?? 0}</Badge>
              <Badge variant="outline">Indexed: {status?.indexed_count ?? 0}</Badge>
              <Badge variant="outline">Total Size: {formatBytes(totalSize)}</Badge>
            </div>
            <p className="text-sm text-muted-foreground">
              Folder: <code>{status?.folder_path ?? '-'}</code>
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Knowledge Settings</CardTitle>
            <CardDescription>
              Configure whether knowledge is enabled, where files are stored, and watcher behavior.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between rounded-md border p-3">
              <div className="space-y-1">
                <p className="text-sm font-medium">Enable Knowledge</p>
                <p className="text-xs text-muted-foreground">
                  Turn knowledge indexing and search on or off globally.
                </p>
              </div>
              <Checkbox
                checked={settings.enabled}
                onCheckedChange={checked => updateSettings({ enabled: checked === true })}
              />
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium" htmlFor="knowledge-path">
                Knowledge Folder Path
              </label>
              <Input
                id="knowledge-path"
                value={settings.path}
                onChange={event => updateSettings({ path: event.target.value })}
                placeholder="./knowledge_docs"
              />
            </div>

            <div className="flex items-center justify-between rounded-md border p-3">
              <div className="space-y-1">
                <p className="text-sm font-medium">Watch Folder</p>
                <p className="text-xs text-muted-foreground">
                  Automatically index file additions and updates.
                </p>
              </div>
              <Checkbox
                checked={settings.watch}
                onCheckedChange={checked => updateSettings({ watch: checked === true })}
              />
            </div>

            <div className="flex justify-end">
              <Button
                variant="outline"
                onClick={handleSaveSettings}
                disabled={savingSettings || !isDirty}
              >
                {savingSettings ? 'Saving...' : 'Save Settings'}
              </Button>
            </div>
          </CardContent>
        </Card>

        {error && (
          <Card className="border-destructive/30">
            <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
          </Card>
        )}

        {!enabled && (
          <Card>
            <CardContent className="py-4 text-sm text-muted-foreground">
              Knowledge is disabled in configuration. Set <code>knowledge.enabled: true</code> to
              enable indexing.
            </CardContent>
          </Card>
        )}

        <Card
          className={cn(
            'border-dashed transition-colors',
            dragActive ? 'border-primary bg-primary/5' : 'border-border'
          )}
          onDragOver={event => {
            event.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={event => {
            event.preventDefault();
            setDragActive(false);
          }}
          onDrop={onDrop}
        >
          <CardContent className="py-6">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
              <div>
                <p className="font-medium">Drop files here or upload manually</p>
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
                  disabled={uploading}
                >
                  <Upload className="h-4 w-4 mr-2" />
                  {uploading ? 'Uploading...' : 'Upload'}
                </Button>
                <Button variant="outline" onClick={handleReindex} disabled={reindexing || !enabled}>
                  <RefreshCw className={cn('h-4 w-4 mr-2', reindexing && 'animate-spin')} />
                  {reindexing ? 'Reindexing...' : 'Reindex'}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card className="flex-1 min-h-0">
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Knowledge Files</CardTitle>
          </CardHeader>
          <CardContent className="h-[calc(100%-4.5rem)] min-h-0">
            <div className="h-full overflow-auto rounded-md border">
              <table className="w-full min-w-[760px] text-sm">
                <thead>
                  {table.getHeaderGroups().map(headerGroup => (
                    <tr key={headerGroup.id} className="border-b bg-muted/50">
                      {headerGroup.headers.map(header => (
                        <th key={header.id} className="px-4 py-2.5 text-left align-middle">
                          {header.isPlaceholder
                            ? null
                            : flexRender(header.column.columnDef.header, header.getContext())}
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
                        No files uploaded yet.
                      </td>
                    </tr>
                  ) : (
                    table.getRowModel().rows.map(row => (
                      <tr key={row.id} className="border-b last:border-b-0">
                        {row.getVisibleCells().map(cell => (
                          <td key={cell.id} className="px-4 py-2.5 align-middle">
                            {flexRender(cell.column.columnDef.cell, cell.getContext())}
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
      </div>
    </div>
  );
}
