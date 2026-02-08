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
import type { KnowledgeBaseConfig } from '@/types/config';
import { useToast } from '@/components/ui/use-toast';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Plus, RefreshCw, Trash2, Upload } from 'lucide-react';

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
}

interface KnowledgeFilesResponse {
  base_id: string;
  files: KnowledgeFile[];
  total_size: number;
  file_count: number;
}

const DEFAULT_BASE_SETTINGS: KnowledgeBaseConfig = {
  path: './knowledge_docs/default',
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

function defaultPathForBase(baseName: string): string {
  return `./knowledge_docs/${baseName}`;
}

function validateBaseName(baseName: string): string | null {
  if (!baseName.trim()) {
    return 'Base name is required';
  }
  if (!/^[a-zA-Z0-9_-]+$/.test(baseName)) {
    return 'Base name can only contain letters, numbers, underscores, and hyphens';
  }
  return null;
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
  const { config, updateKnowledgeBase, deleteKnowledgeBase, saveConfig, isDirty } =
    useConfigStore();
  const { toast } = useToast();

  const [selectedBase, setSelectedBase] = useState<string>('');
  const [newBaseName, setNewBaseName] = useState('');
  const [files, setFiles] = useState<KnowledgeFile[]>([]);
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [settings, setSettings] = useState<KnowledgeBaseConfig>(DEFAULT_BASE_SETTINGS);
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

  const knowledgeBases = config?.knowledge_bases || {};
  const baseNames = useMemo(() => Object.keys(knowledgeBases).sort(), [knowledgeBases]);

  useEffect(() => {
    if (baseNames.length === 0) {
      if (selectedBase !== '') {
        setSelectedBase('');
      }
      return;
    }

    if (!selectedBase || !baseNames.includes(selectedBase)) {
      setSelectedBase(baseNames[0]);
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

    setSettings({
      path: selectedConfig.path,
      watch: selectedConfig.watch,
    });
  }, [knowledgeBases, selectedBase]);

  const loadData = useCallback(async (baseId: string | null) => {
    setLoading(true);
    setError(null);

    try {
      if (!baseId) {
        setFiles([]);
        setStatus(null);
        setTotalSize(0);
        return;
      }

      const [statusData, filesData] = await Promise.all([
        fetchJson<KnowledgeStatus>(API_ENDPOINTS.knowledge.status(baseId)),
        fetchJson<KnowledgeFilesResponse>(API_ENDPOINTS.knowledge.files(baseId)),
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
    void loadData(selectedBase || null);
  }, [selectedBase, loadData]);

  const updateSettings = useCallback(
    (updates: Partial<KnowledgeBaseConfig>) => {
      if (!selectedBase) {
        return;
      }

      setSettings(previous => {
        const next = { ...previous, ...updates };
        updateKnowledgeBase(selectedBase, next);
        return next;
      });
    },
    [selectedBase, updateKnowledgeBase]
  );

  const handleSaveSettings = useCallback(async () => {
    setSavingSettings(true);
    setError(null);
    try {
      await saveConfig();
      await loadData(selectedBase || null);
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
  }, [loadData, saveConfig, selectedBase, toast]);

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
      updateKnowledgeBase(baseName, {
        path: defaultPathForBase(baseName),
        watch: true,
      });
      await saveConfig();
      setSelectedBase(baseName);
      setNewBaseName('');
      await loadData(baseName);
      toast({
        title: 'Knowledge base created',
        description: `Base '${baseName}' is ready for uploads.`,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to create knowledge base';
      setError(message);
      toast({
        title: 'Create failed',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setCreatingBase(false);
    }
  }, [baseNames, updateKnowledgeBase, loadData, newBaseName, saveConfig, toast]);

  const handleDeleteBase = useCallback(async () => {
    if (!selectedBase) {
      return;
    }

    if (!window.confirm(`Delete knowledge base '${selectedBase}'?`)) {
      return;
    }

    const nextBase = baseNames.filter(name => name !== selectedBase)[0] || null;

    setDeletingBase(true);
    setError(null);

    try {
      deleteKnowledgeBase(selectedBase);
      await saveConfig();
      setSelectedBase(nextBase || '');
      await loadData(nextBase);
      toast({
        title: 'Knowledge base deleted',
        description: `Deleted '${selectedBase}'.`,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to delete knowledge base';
      setError(message);
      toast({
        title: 'Delete failed',
        description: message,
        variant: 'destructive',
      });
    } finally {
      setDeletingBase(false);
    }
  }, [baseNames, deleteKnowledgeBase, loadData, saveConfig, selectedBase, toast]);

  const uploadFiles = useCallback(
    async (selectedFiles: File[]) => {
      if (selectedFiles.length === 0 || !selectedBase) {
        return;
      }

      const formData = new FormData();
      selectedFiles.forEach(file => {
        formData.append('files', file);
      });

      setUploading(true);
      setError(null);

      try {
        await fetchJson<{ uploaded: string[] }>(API_ENDPOINTS.knowledge.upload(selectedBase), {
          method: 'POST',
          body: formData,
        });
        await loadData(selectedBase);
        toast({
          title: 'Upload complete',
          description: `Uploaded ${selectedFiles.length} file${
            selectedFiles.length === 1 ? '' : 's'
          } to '${selectedBase}'.`,
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
    [loadData, selectedBase, toast]
  );

  const handleDeleteFile = useCallback(
    async (path: string) => {
      if (!selectedBase) {
        return;
      }

      if (!window.confirm(`Delete '${path}' from knowledge base '${selectedBase}'?`)) {
        return;
      }

      setDeletingPath(path);
      setError(null);

      try {
        await fetchJson<{ success: boolean }>(
          API_ENDPOINTS.knowledge.deleteFile(selectedBase, path),
          {
            method: 'DELETE',
          }
        );
        await loadData(selectedBase);
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
    [loadData, selectedBase, toast]
  );

  const handleReindex = useCallback(async () => {
    if (!selectedBase) {
      return;
    }

    setReindexing(true);
    setError(null);

    try {
      await fetchJson<{ indexed_count: number }>(API_ENDPOINTS.knowledge.reindex(selectedBase), {
        method: 'POST',
      });
      await loadData(selectedBase);
      toast({
        title: 'Reindex complete',
        description: `Knowledge base '${selectedBase}' rebuilt successfully.`,
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
  }, [loadData, selectedBase, toast]);

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
              onClick={() => handleDeleteFile(row.original.path)}
              disabled={deletingPath === row.original.path || isDirty}
              title="Delete file"
            >
              <Trash2 className="h-4 w-4 text-destructive" />
            </Button>
          </div>
        ),
      },
    ],
    [deletingPath, handleDeleteFile, isDirty]
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

  return (
    <div className="h-full overflow-hidden">
      <div className="h-full flex flex-col gap-4">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-xl">Knowledge Bases</CardTitle>
            <CardDescription>
              Manage separate knowledge bases and assign agents to a specific base.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline">Configured Bases: {baseNames.length}</Badge>
              {selectedBase && <Badge variant="default">Active: {selectedBase}</Badge>}
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-[1fr_auto] gap-3">
              <Select value={selectedBase || undefined} onValueChange={setSelectedBase}>
                <SelectTrigger>
                  <SelectValue placeholder="Select a knowledge base" />
                </SelectTrigger>
                <SelectContent>
                  {baseNames.map(baseName => (
                    <SelectItem key={baseName} value={baseName}>
                      {baseName}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              <Button
                variant="outline"
                onClick={handleDeleteBase}
                disabled={!selectedBase || deletingBase || isDirty}
              >
                <Trash2 className="h-4 w-4 mr-2" />
                {deletingBase ? 'Deleting...' : 'Delete Base'}
              </Button>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-[1fr_auto] gap-3">
              <Input
                value={newBaseName}
                onChange={event => setNewBaseName(event.target.value)}
                placeholder="new_base_name"
              />
              <Button
                variant="outline"
                onClick={handleCreateBase}
                disabled={creatingBase || isDirty}
              >
                <Plus className="h-4 w-4 mr-2" />
                {creatingBase ? 'Creating...' : 'Add Base'}
              </Button>
            </div>
          </CardContent>
        </Card>

        {selectedBase ? (
          <>
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Base Settings</CardTitle>
                <CardDescription>
                  Configure folder path and watcher behavior for <code>{selectedBase}</code>.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <label className="text-sm font-medium" htmlFor="knowledge-path">
                    Folder Path
                  </label>
                  <Input
                    id="knowledge-path"
                    value={settings.path}
                    onChange={event => updateSettings({ path: event.target.value })}
                    placeholder={defaultPathForBase(selectedBase)}
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

            <Card>
              <CardContent className="py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline">Files: {status?.file_count ?? 0}</Badge>
                  <Badge variant="outline">Indexed: {status?.indexed_count ?? 0}</Badge>
                  <Badge variant="outline">Total Size: {formatBytes(totalSize)}</Badge>
                </div>
                <p className="text-sm text-muted-foreground mt-2">
                  Folder: <code>{status?.folder_path ?? '-'}</code>
                </p>
              </CardContent>
            </Card>
          </>
        ) : (
          <Card>
            <CardContent className="py-4 text-sm text-muted-foreground">
              No knowledge bases configured yet. Add one above to start uploading files.
            </CardContent>
          </Card>
        )}

        {isDirty && (
          <Card className="border-amber-500/30">
            <CardContent className="py-3 text-sm text-amber-700 dark:text-amber-300">
              Save settings before uploading, deleting, or reindexing files.
            </CardContent>
          </Card>
        )}

        {error && (
          <Card className="border-destructive/30">
            <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
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
                  disabled={uploading || !selectedBase || isDirty}
                >
                  <Upload className="h-4 w-4 mr-2" />
                  {uploading ? 'Uploading...' : 'Upload'}
                </Button>
                <Button
                  variant="outline"
                  onClick={handleReindex}
                  disabled={reindexing || !selectedBase || isDirty}
                >
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
