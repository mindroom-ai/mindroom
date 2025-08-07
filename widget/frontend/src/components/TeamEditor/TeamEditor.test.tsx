import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { TeamEditor } from './TeamEditor';
import { useConfigStore } from '@/store/configStore';
import { Team, Agent, Config } from '@/types/config';

// Mock the store
vi.mock('@/store/configStore');

describe('TeamEditor', () => {
  const mockTeam: Team = {
    id: 'dev_team',
    display_name: 'Dev Team',
    role: 'Development team for coding tasks',
    agents: ['code', 'shell'],
    rooms: ['dev', 'lobby'],
    mode: 'coordinate',
    model: 'default',
  };

  const mockAgents: Agent[] = [
    {
      id: 'code',
      display_name: 'Code Agent',
      role: 'Writes code',
      tools: ['file', 'shell'],
      instructions: [],
      rooms: ['dev'],
      num_history_runs: 5,
    },
    {
      id: 'shell',
      display_name: 'Shell Agent',
      role: 'Executes commands',
      tools: ['shell'],
      instructions: [],
      rooms: ['dev'],
      num_history_runs: 5,
    },
    {
      id: 'research',
      display_name: 'Research Agent',
      role: 'Conducts research',
      tools: ['duckduckgo', 'wikipedia'],
      instructions: [],
      rooms: ['research'],
      num_history_runs: 5,
    },
  ];

  const mockConfig: Partial<Config> = {
    models: {
      default: { provider: 'ollama', id: 'llama2' },
      gpt4: { provider: 'openai', id: 'gpt-4' },
      claude: { provider: 'anthropic', id: 'claude-3' },
    },
  };

  const mockUpdateTeam = vi.fn();
  const mockDeleteTeam = vi.fn();
  const mockSaveConfig = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      teams: [mockTeam],
      agents: mockAgents,
      selectedTeamId: 'dev_team',
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      config: mockConfig,
      isDirty: false,
    });
  });

  it('renders team editor with team details', () => {
    render(<TeamEditor />);

    expect(screen.getByDisplayValue('Dev Team')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Development team for coding tasks')).toBeInTheDocument();
    expect(screen.getByText('Team Details')).toBeInTheDocument();
  });

  it('shows placeholder when no team is selected', () => {
    (useConfigStore as any).mockReturnValue({
      teams: [],
      agents: mockAgents,
      selectedTeamId: null,
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      config: mockConfig,
      isDirty: false,
    });

    render(<TeamEditor />);

    expect(screen.getByText('Select a team to edit')).toBeInTheDocument();
  });

  it('updates team display name', async () => {
    render(<TeamEditor />);

    const nameInput = screen.getByLabelText('Display Name');
    fireEvent.change(nameInput, { target: { value: 'Updated Team Name' } });

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        display_name: 'Updated Team Name',
      });
    });
  });

  it('updates team role description', async () => {
    render(<TeamEditor />);

    const roleInput = screen.getByLabelText('Team Purpose');
    fireEvent.change(roleInput, { target: { value: 'Updated team purpose' } });

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        role: 'Updated team purpose',
      });
    });
  });

  it('changes collaboration mode', async () => {
    render(<TeamEditor />);

    const modeSelect = screen.getByLabelText('Collaboration Mode');
    fireEvent.click(modeSelect);

    const collaborateOption = await screen.findByText(/Collaborate \(Parallel/);
    fireEvent.click(collaborateOption);

    expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
      mode: 'collaborate',
    });
  });

  it('displays team members with checkboxes', () => {
    render(<TeamEditor />);

    expect(screen.getByText('Code Agent')).toBeInTheDocument();
    expect(screen.getByText('Shell Agent')).toBeInTheDocument();
    expect(screen.getByText('Research Agent')).toBeInTheDocument();

    // Code and Shell should be checked
    const codeCheckbox = screen.getByRole('checkbox', { name: /Code Agent/ });
    const shellCheckbox = screen.getByRole('checkbox', { name: /Shell Agent/ });
    const researchCheckbox = screen.getByRole('checkbox', { name: /Research Agent/ });

    expect(codeCheckbox).toBeChecked();
    expect(shellCheckbox).toBeChecked();
    expect(researchCheckbox).not.toBeChecked();
  });

  it('adds agent to team when checkbox is checked', async () => {
    render(<TeamEditor />);

    const researchCheckbox = screen.getByRole('checkbox', { name: /Research Agent/ });
    fireEvent.click(researchCheckbox);

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        agents: ['code', 'shell', 'research'],
      });
    });
  });

  it('removes agent from team when checkbox is unchecked', async () => {
    render(<TeamEditor />);

    const codeCheckbox = screen.getByRole('checkbox', { name: /Code Agent/ });
    fireEvent.click(codeCheckbox);

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        agents: ['shell'],
      });
    });
  });

  it('adds new room to team', async () => {
    render(<TeamEditor />);

    const addButton = screen.getByRole('button', { name: /Add/i });
    fireEvent.click(addButton);

    // Change the default "new_room" value
    const newRoomInput = screen.getAllByPlaceholderText('Room name...')[2]; // After dev and lobby
    fireEvent.change(newRoomInput, { target: { value: 'testing' } });

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        rooms: ['dev', 'lobby', 'testing'],
      });
    });
  });

  it('removes room from team', async () => {
    render(<TeamEditor />);

    // Find the remove button for the first room
    const removeButtons = screen
      .getAllByRole('button')
      .filter(btn => btn.querySelector('svg')?.classList.contains('h-4'));
    fireEvent.click(removeButtons[0]); // Remove first room

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        rooms: ['lobby'],
      });
    });
  });

  it('changes team model', async () => {
    render(<TeamEditor />);

    const modelSelect = screen.getByLabelText('Team Model (Optional)');
    fireEvent.click(modelSelect);

    const gpt4Option = await screen.findByText('gpt4');
    fireEvent.click(gpt4Option);

    expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
      model: 'gpt4',
    });
  });

  it('sets model to undefined when default is selected', async () => {
    render(<TeamEditor />);

    const modelSelect = screen.getByLabelText('Team Model (Optional)');
    fireEvent.click(modelSelect);

    const defaultOption = await screen.findByText('Use default model');
    fireEvent.click(defaultOption);

    expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
      model: undefined,
    });
  });

  it('calls deleteTeam when delete button is clicked', async () => {
    window.confirm = vi.fn(() => true);
    render(<TeamEditor />);

    const deleteButton = screen.getByRole('button', { name: /Delete/i });
    fireEvent.click(deleteButton);

    expect(window.confirm).toHaveBeenCalledWith('Are you sure you want to delete this team?');
    expect(mockDeleteTeam).toHaveBeenCalledWith('dev_team');
  });

  it('does not delete team when confirm is cancelled', () => {
    window.confirm = vi.fn(() => false);
    render(<TeamEditor />);

    const deleteButton = screen.getByRole('button', { name: /Delete/i });
    fireEvent.click(deleteButton);

    expect(mockDeleteTeam).not.toHaveBeenCalled();
  });

  it('calls saveConfig when save button is clicked', async () => {
    render(<TeamEditor />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalled();
    });
  });

  it('disables save button when not dirty', () => {
    render(<TeamEditor />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).toBeDisabled();
  });

  it('enables save button when dirty', () => {
    (useConfigStore as any).mockReturnValue({
      teams: [mockTeam],
      agents: mockAgents,
      selectedTeamId: 'dev_team',
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      config: mockConfig,
      isDirty: true,
    });

    render(<TeamEditor />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).not.toBeDisabled();
  });
});
