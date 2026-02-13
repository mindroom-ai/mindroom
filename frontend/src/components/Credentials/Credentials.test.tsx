import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Credentials } from './Credentials';

const mockToast = vi.fn();

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

global.fetch = vi.fn();

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

describe('Credentials', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
  });

  it('loads services and selected service credentials', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: true,
          key_names: ['username', 'token'],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: { username: 'x-access-token', token: 'ghp_test' },
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect(screen.getByText('Configured')).toBeInTheDocument();
      expect(screen.getByText('Keys: username, token')).toBeInTheDocument();
    });

    const editor = screen.getByPlaceholderText('{"api_key":"..."}') as HTMLTextAreaElement;
    await waitFor(() => {
      expect(editor.value).toContain('"username": "x-access-token"');
      expect(editor.value).toContain('"token": "ghp_test"');
    });
  });

  it('hides credentials by default and reveals them on demand', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: true,
          key_names: ['token'],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: { token: 'ghp_test' },
        }),
      });

    render(<Credentials />);

    const editor = screen.getByPlaceholderText('{"api_key":"..."}') as HTMLTextAreaElement;
    await waitFor(() => {
      expect(editor.value).toContain('"token": "ghp_test"');
      expect(editor.className).toContain('blur-sm');
      expect(
        screen.getByText('Credentials hidden for screen sharing. Click Show to reveal.')
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Show' }));

    await waitFor(() => {
      expect(editor.className).not.toContain('blur-sm');
      expect(
        screen.queryByText('Credentials hidden for screen sharing. Click Show to reveal.')
      ).toBeNull();
      expect(screen.getByRole('button', { name: 'Hide' })).toBeInTheDocument();
    });
  });

  it('saves credentials JSON for selected service', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: {},
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          status: 'success',
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: true,
          key_names: ['username', 'token'],
        }),
      });

    render(<Credentials />);

    await screen.findByText('Empty');
    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    const editor = screen.getByPlaceholderText('{"api_key":"..."}');
    fireEvent.change(editor, {
      target: { value: '{"username":"x-access-token","token":"ghp_updated"}' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:8765/api/credentials/github_private',
        expect.objectContaining({
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            credentials: { username: 'x-access-token', token: 'ghp_updated' },
          }),
        })
      );
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Credentials saved',
        description: "Updated credentials for 'github_private'.",
      });
    });
  });

  it('shows error and does not save when JSON is invalid', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: {},
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    fireEvent.change(screen.getByPlaceholderText('{"api_key":"..."}'), {
      target: { value: '{"broken_json": ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(screen.getByText('Credentials must be valid JSON')).toBeInTheDocument();
    });
    expect((global.fetch as any).mock.calls).toHaveLength(3);
  });

  it('deletes selected service credentials', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: true,
          key_names: ['token'],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: { token: 'ghp_test' },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          status: 'success',
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalledWith("Delete credentials for 'github_private'?");
      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:8765/api/credentials/github_private',
        expect.objectContaining({
          method: 'DELETE',
        })
      );
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Credentials deleted',
        description: "Removed credentials for 'github_private'.",
      });
    });
  });

  it('tests credentials for selected service', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['github_private'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          has_credentials: true,
          key_names: ['token'],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'github_private',
          credentials: { token: 'ghp_test' },
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          message: 'Credentials exist (validation not implemented)',
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(3);
    });

    fireEvent.click(screen.getByRole('button', { name: 'Test' }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        'http://localhost:8765/api/credentials/github_private/test',
        expect.objectContaining({
          method: 'POST',
        })
      );
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Credentials check',
        description: 'Credentials exist (validation not implemented)',
      });
    });
  });

  it('shows error when creating a service with an invalid name', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => [],
    });

    render(<Credentials />);

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(1);
    });

    fireEvent.change(screen.getByPlaceholderText('new_service_name'), {
      target: { value: 'bad/name' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => {
      expect(
        screen.getByText(
          'Service name can only include letters, numbers, colon, underscore, and hyphen'
        )
      ).toBeInTheDocument();
    });
  });

  it('continues loading services when one status request fails', async () => {
    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['good_service', 'zbad_service'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'good_service',
          has_credentials: true,
          key_names: ['token'],
        }),
      })
      .mockRejectedValueOnce(new Error('Status endpoint failed'))
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'good_service',
          credentials: { token: 'abc' },
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect(screen.getByText('good_service')).toBeInTheDocument();
      expect(screen.getByText('zbad_service')).toBeInTheDocument();
      expect(
        screen.getByText(
          'Some service statuses could not be loaded. You can still edit credentials.'
        )
      ).toBeInTheDocument();
    });
  });

  it('prompts before switching services with unsaved changes', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['service_a', 'service_b'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'service_a',
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'service_b',
          has_credentials: false,
          key_names: [],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'service_a',
          credentials: {},
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect((global.fetch as any).mock.calls).toHaveLength(4);
    });

    fireEvent.change(screen.getByPlaceholderText('{"api_key":"..."}'), {
      target: { value: '{"token":"draft"}' },
    });
    fireEvent.click(screen.getByRole('button', { name: /service_b/i }));

    expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes for 'service_a'?");
    expect((global.fetch as any).mock.calls).toHaveLength(4);
  });

  it('ignores stale credentials response from previously selected service', async () => {
    const firstCredentials = deferred<any>();

    (global.fetch as any)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ['service_a', 'service_b'],
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'service_a',
          has_credentials: true,
          key_names: ['token'],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'service_b',
          has_credentials: true,
          key_names: ['token'],
        }),
      })
      .mockImplementationOnce(() => firstCredentials.promise)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          service: 'service_b',
          credentials: { token: 'b-value' },
        }),
      });

    render(<Credentials />);

    await waitFor(() => {
      expect(screen.getByText('service_b')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /service_b/i }));

    const editor = screen.getByPlaceholderText('{"api_key":"..."}') as HTMLTextAreaElement;
    await waitFor(() => {
      expect(editor.value).toContain('"token": "b-value"');
    });

    firstCredentials.resolve({
      ok: true,
      json: async () => ({
        service: 'service_a',
        credentials: { token: 'a-value' },
      }),
    });

    await waitFor(() => {
      expect(editor.value).toContain('"token": "b-value"');
      expect(editor.value).not.toContain('"token": "a-value"');
    });
  });
});
