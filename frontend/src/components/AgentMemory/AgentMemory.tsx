import { useEffect, useMemo, useState } from 'react';
import {
  getWorkspaceFile,
  getWorkspaceFiles,
  updateWorkspaceFile,
  type WorkspaceFileMetadata,
} from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';

type AgentMemoryProps = {
  agentId: string;
};

const DAILY_LOG_FILE_PATTERN = /^memory(?:\/[A-Za-z0-9._-]+)*\/\d{4}-\d{2}-\d{2}\.md$/;

export function AgentMemory({ agentId }: AgentMemoryProps) {
  const [files, setFiles] = useState<WorkspaceFileMetadata[]>([]);
  const [selectedFilename, setSelectedFilename] = useState<string>('');
  const [content, setContent] = useState('');
  const [etag, setEtag] = useState('');
  const [status, setStatus] = useState('');
  const [loadingList, setLoadingList] = useState(false);
  const [loadingFile, setLoadingFile] = useState(false);
  const [saving, setSaving] = useState(false);

  const isDailyLog = useMemo(
    () => selectedFilename.length > 0 && DAILY_LOG_FILE_PATTERN.test(selectedFilename),
    [selectedFilename]
  );

  const loadFiles = async () => {
    setLoadingList(true);
    setStatus('');
    try {
      const payload = await getWorkspaceFiles(agentId);
      setFiles(payload.files);
      if (payload.files.length === 0) {
        setSelectedFilename('');
        setContent('');
        setEtag('');
        return;
      }
      const stillSelected = payload.files.find(file => file.filename === selectedFilename);
      setSelectedFilename(stillSelected ? stillSelected.filename : payload.files[0].filename);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Failed to load workspace files');
    } finally {
      setLoadingList(false);
    }
  };

  const loadFile = async (filename: string) => {
    setLoadingFile(true);
    setStatus('');
    try {
      const payload = await getWorkspaceFile(agentId, filename);
      setContent(payload.file.content);
      setEtag(payload.etag);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Failed to load file');
    } finally {
      setLoadingFile(false);
    }
  };

  const handleSave = async () => {
    if (!selectedFilename || isDailyLog) {
      return;
    }
    setSaving(true);
    setStatus('');
    try {
      const payload = await updateWorkspaceFile(agentId, selectedFilename, content, etag);
      setEtag(payload.etag);
      setStatus('Saved');
      setFiles(current =>
        current.map(file =>
          file.filename === payload.file.filename
            ? {
                ...file,
                size_bytes: payload.file.size_bytes,
                last_modified: payload.file.last_modified,
              }
            : file
        )
      );
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Failed to save file');
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => {
    setSelectedFilename('');
    setContent('');
    setEtag('');
    void loadFiles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  useEffect(() => {
    if (!selectedFilename) {
      return;
    }
    void loadFile(selectedFilename);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, selectedFilename]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">Edit SOUL.md, AGENTS.md, and MEMORY.md.</p>
        <Button variant="outline" size="sm" onClick={() => void loadFiles()} disabled={loadingList}>
          {loadingList ? 'Refreshing...' : 'Refresh'}
        </Button>
      </div>

      {status && <p className="text-sm text-muted-foreground">{status}</p>}

      <div className="grid gap-4 lg:grid-cols-[260px_1fr]">
        <div className="max-h-[420px] overflow-y-auto rounded-lg border p-2">
          {files.length === 0 && !loadingList && (
            <p className="px-2 py-1 text-sm text-muted-foreground">No workspace files.</p>
          )}
          {files.map(file => (
            <button
              key={file.filename}
              type="button"
              className={`mb-1 block w-full rounded px-2 py-1 text-left text-sm transition-colors ${
                selectedFilename === file.filename
                  ? 'bg-primary/15 text-primary'
                  : 'hover:bg-muted text-foreground'
              }`}
              onClick={() => setSelectedFilename(file.filename)}
            >
              <div className="truncate font-medium">{file.filename}</div>
              <div className="text-xs text-muted-foreground">{file.size_bytes} bytes</div>
            </button>
          ))}
        </div>

        <div className="space-y-2">
          <Textarea
            value={content}
            onChange={event => setContent(event.target.value)}
            placeholder={selectedFilename ? 'Select a file to edit' : 'No file selected'}
            className="min-h-[420px] font-mono text-sm"
            readOnly={isDailyLog || loadingFile || !selectedFilename}
          />
          <div className="flex items-center gap-2">
            <Button
              onClick={() => void handleSave()}
              disabled={!selectedFilename || isDailyLog || saving}
            >
              {saving ? 'Saving...' : 'Save'}
            </Button>
            {isDailyLog && (
              <p className="text-xs text-muted-foreground">
                Daily logs are read-only; they are appended automatically.
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
