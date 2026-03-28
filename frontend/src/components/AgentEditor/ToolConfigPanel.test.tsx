import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ToolConfigPanel } from './ToolConfigPanel';

vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

import { useConfigStore } from '@/store/configStore';

describe('ToolConfigPanel', () => {
  const mockStore = {
    getAgentToolOverrides: vi.fn(),
    updateAgentToolOverrides: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockStore.getAgentToolOverrides.mockReturnValue({
      extra_env_passthrough: ['GITEA_TOKEN'],
    });
    vi.mocked(useConfigStore).mockReturnValue(mockStore as never);
  });

  it('renders an empty state when no tool is selected', () => {
    render(<ToolConfigPanel agentId="openclaw" toolName={null} />);

    expect(
      screen.getByText('Select a checked tool to edit per-agent settings.')
    ).toBeInTheDocument();
  });

  it('renders a generic key-value editor when the tool has no override fields', () => {
    mockStore.getAgentToolOverrides.mockReturnValue(null);
    render(
      <ToolConfigPanel
        agentId="openclaw"
        toolName="browser"
        toolDisplayName="Browser"
        fields={null}
      />
    );

    expect(screen.getByText('Browser settings')).toBeInTheDocument();
    expect(screen.getByText('Add override')).toBeInTheDocument();
    expect(
      screen.getByText('No overrides configured. Add a key-value pair to customize this tool.')
    ).toBeInTheDocument();
  });

  it('renders string-array fields and pushes normalized updates back to the store', () => {
    render(
      <ToolConfigPanel
        agentId="openclaw"
        toolName="shell"
        toolDisplayName="Shell Commands"
        fields={[
          {
            name: 'extra_env_passthrough',
            label: 'Env Passthrough',
            type: 'string[]',
            description: 'Extra env vars exposed to shell execution.',
          },
          {
            name: 'shell_path_prepend',
            label: 'PATH Prepend',
            type: 'string[]',
            description: 'Path entries prepended to PATH.',
          },
        ]}
      />
    );

    expect(screen.getByText('Shell Commands settings')).toBeInTheDocument();
    expect(screen.getByDisplayValue('GITEA_TOKEN')).toBeInTheDocument();
    expect(screen.getByText('Customized')).toBeInTheDocument();

    fireEvent.click(screen.getAllByText('Add value')[1]);
    fireEvent.change(screen.getByPlaceholderText('PATH Prepend'), {
      target: { value: '/run/wrappers/bin' },
    });

    expect(mockStore.updateAgentToolOverrides).toHaveBeenLastCalledWith('openclaw', 'shell', {
      extra_env_passthrough: ['GITEA_TOKEN'],
      shell_path_prepend: ['/run/wrappers/bin'],
    });

    fireEvent.click(screen.getByLabelText('Remove Env Passthrough value 1'));

    expect(mockStore.updateAgentToolOverrides).toHaveBeenLastCalledWith('openclaw', 'shell', {
      extra_env_passthrough: null,
      shell_path_prepend: ['/run/wrappers/bin'],
    });
  });
});
