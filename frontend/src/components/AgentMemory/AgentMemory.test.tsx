import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AgentMemory } from './AgentMemory';
import {
  getWorkspaceFile,
  getWorkspaceFiles,
  updateWorkspaceFile,
  type WorkspaceFileResponse,
} from '@/lib/api';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getWorkspaceFiles: vi.fn(),
    getWorkspaceFile: vi.fn(),
    updateWorkspaceFile: vi.fn(),
  };
});

describe('AgentMemory', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads files and saves editable workspace files', async () => {
    vi.mocked(getWorkspaceFiles).mockResolvedValue({
      agent_name: 'test_agent',
      files: [
        {
          filename: 'SOUL.md',
          size_bytes: 10,
          last_modified: '2026-02-16T00:00:00Z',
          agent_name: 'test_agent',
        },
      ],
      count: 1,
    });

    vi.mocked(getWorkspaceFile).mockResolvedValue({
      file: {
        filename: 'SOUL.md',
        content: 'original soul',
        size_bytes: 10,
        last_modified: '2026-02-16T00:00:00Z',
        agent_name: 'test_agent',
      },
      etag: '"etag-1"',
    });

    vi.mocked(updateWorkspaceFile).mockResolvedValue({
      file: {
        filename: 'SOUL.md',
        content: 'updated soul',
        size_bytes: 12,
        last_modified: '2026-02-16T00:05:00Z',
        agent_name: 'test_agent',
      },
      etag: '"etag-2"',
    });

    render(<AgentMemory agentId="test_agent" />);

    await screen.findByText('SOUL.md');
    const textarea = screen.getByRole('textbox');
    await waitFor(() => expect(textarea).toHaveValue('original soul'));

    fireEvent.change(textarea, { target: { value: 'updated soul' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(updateWorkspaceFile).toHaveBeenCalledWith(
        'test_agent',
        'SOUL.md',
        'updated soul',
        '"etag-1"'
      )
    );
  });

  it('marks daily logs as read-only', async () => {
    vi.mocked(getWorkspaceFiles).mockResolvedValue({
      agent_name: 'test_agent',
      files: [
        {
          filename: 'memory/2026-02-16.md',
          size_bytes: 12,
          last_modified: '2026-02-16T00:00:00Z',
          agent_name: 'test_agent',
        },
      ],
      count: 1,
    });

    const dailyFile: WorkspaceFileResponse = {
      filename: 'memory/2026-02-16.md',
      content: 'daily log',
      size_bytes: 12,
      last_modified: '2026-02-16T00:00:00Z',
      agent_name: 'test_agent',
    };
    vi.mocked(getWorkspaceFile).mockResolvedValue({ file: dailyFile, etag: '"daily"' });

    render(<AgentMemory agentId="test_agent" />);

    await screen.findByText('memory/2026-02-16.md');
    await waitFor(() => expect(screen.getByRole('textbox')).toHaveValue('daily log'));

    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.getByText(/Daily logs are read-only/)).toBeInTheDocument();
  });

  it('ignores stale file responses when selection changes quickly', async () => {
    vi.mocked(getWorkspaceFiles).mockResolvedValue({
      agent_name: 'test_agent',
      files: [
        {
          filename: 'SOUL.md',
          size_bytes: 10,
          last_modified: '2026-02-16T00:00:00Z',
          agent_name: 'test_agent',
        },
        {
          filename: 'AGENTS.md',
          size_bytes: 14,
          last_modified: '2026-02-16T00:00:00Z',
          agent_name: 'test_agent',
        },
      ],
      count: 2,
    });

    const soulRequest = deferred<{ file: WorkspaceFileResponse; etag: string }>();
    const agentsRequest = deferred<{ file: WorkspaceFileResponse; etag: string }>();

    vi.mocked(getWorkspaceFile).mockImplementation((_agentId, filename) => {
      if (filename === 'SOUL.md') {
        return soulRequest.promise;
      }
      if (filename === 'AGENTS.md') {
        return agentsRequest.promise;
      }
      throw new Error(`Unexpected filename: ${filename}`);
    });

    render(<AgentMemory agentId="test_agent" />);

    await screen.findByText('SOUL.md');
    const agentsButton = screen.getByText('AGENTS.md').closest('button');
    expect(agentsButton).not.toBeNull();
    if (!agentsButton) {
      throw new Error('AGENTS button not found');
    }
    fireEvent.click(agentsButton);

    agentsRequest.resolve({
      file: {
        filename: 'AGENTS.md',
        content: 'agents content',
        size_bytes: 14,
        last_modified: '2026-02-16T00:00:00Z',
        agent_name: 'test_agent',
      },
      etag: '"agents-etag"',
    });

    await waitFor(() => expect(screen.getByRole('textbox')).toHaveValue('agents content'));

    soulRequest.resolve({
      file: {
        filename: 'SOUL.md',
        content: 'stale soul content',
        size_bytes: 10,
        last_modified: '2026-02-16T00:00:00Z',
        agent_name: 'test_agent',
      },
      etag: '"soul-etag"',
    });

    await waitFor(() => expect(screen.getByRole('textbox')).toHaveValue('agents content'));
  });
});
