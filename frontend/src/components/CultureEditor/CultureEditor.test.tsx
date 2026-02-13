import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CultureEditor } from './CultureEditor';
import { useConfigStore } from '@/store/configStore';

const mockToast = vi.fn();

vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

describe('CultureEditor', () => {
  const mockUpdateCulture = vi.fn();
  const mockDeleteCulture = vi.fn();
  const mockSaveConfig = vi.fn();
  const mockSelectCulture = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      cultures: [
        {
          id: 'engineering',
          description: 'Shared engineering best practices',
          agents: ['code'],
          mode: 'automatic',
        },
        {
          id: 'support',
          description: 'Support standards',
          agents: ['general'],
          mode: 'manual',
        },
      ],
      agents: [
        {
          id: 'code',
          display_name: 'Code Agent',
          role: 'Writes and reviews code',
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
        {
          id: 'general',
          display_name: 'General Agent',
          role: 'Answers general questions',
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
        {
          id: 'data',
          display_name: 'Data Agent',
          role: 'Analyzes data',
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
      ],
      selectedCultureId: 'engineering',
      updateCulture: mockUpdateCulture,
      deleteCulture: mockDeleteCulture,
      saveConfig: mockSaveConfig,
      isDirty: false,
      selectCulture: mockSelectCulture,
    });
  });

  it('shows current culture assignment in the agent list', () => {
    render(<CultureEditor />);

    expect(screen.getByText('Currently in: this culture')).toBeInTheDocument();
    expect(screen.getByText('Currently in: support')).toBeInTheDocument();
    expect(screen.getByText('Currently in: none')).toBeInTheDocument();
  });

  it('toasts when assigning an agent currently in another culture', async () => {
    render(<CultureEditor />);

    const generalCheckbox = screen.getByRole('checkbox', { name: /General Agent/i });
    fireEvent.click(generalCheckbox);

    await waitFor(() => {
      expect(mockUpdateCulture).toHaveBeenCalledWith('engineering', {
        agents: ['code', 'general'],
      });
    });

    expect(mockToast).toHaveBeenCalledWith({
      title: 'Agent moved to culture',
      description: 'General Agent moved from support to engineering.',
    });
  });

  it('does not toast when assigning an unassigned agent', () => {
    render(<CultureEditor />);

    const dataCheckbox = screen.getByRole('checkbox', { name: /Data Agent/i });
    fireEvent.click(dataCheckbox);

    expect(mockToast).not.toHaveBeenCalled();
  });
});
