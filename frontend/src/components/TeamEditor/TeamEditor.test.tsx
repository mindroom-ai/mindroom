import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { TeamEditor } from './TeamEditor';
import { useConfigStore } from '@/store/configStore';
import { Team, Agent, AgentPoliciesByAgent, Config, normalizeTeamUpdates } from '@/types/config';

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
    num_history_runs: 6,
  };

  const mockAgents: Agent[] = [
    {
      id: 'code',
      display_name: 'Code Agent',
      role: 'Writes code',
      tools: ['file', 'shell'],
      skills: [],
      instructions: [],
      rooms: ['dev'],
    },
    {
      id: 'shell',
      display_name: 'Shell Agent',
      role: 'Executes commands',
      tools: ['shell'],
      skills: [],
      instructions: [],
      rooms: ['dev'],
    },
    {
      id: 'research',
      display_name: 'Research Agent',
      role: 'Conducts research',
      tools: ['duckduckgo', 'wikipedia'],
      skills: [],
      instructions: [],
      rooms: ['research'],
    },
    {
      id: 'leader',
      display_name: 'Leader Agent',
      role: 'Coordinates work',
      tools: [],
      skills: [],
      instructions: [],
      rooms: ['dev'],
      delegate_to: ['mind'],
    },
    {
      id: 'mind',
      display_name: 'Mind Agent',
      role: 'Private assistant',
      tools: [],
      skills: [],
      instructions: [],
      rooms: ['research'],
      private: { per: 'user' },
    },
  ];

  const mockConfig: Partial<Config> = {
    models: {
      default: { provider: 'ollama', id: 'llama2' },
      gpt4: { provider: 'openai', id: 'gpt-4' },
      claude: { provider: 'anthropic', id: 'claude-3' },
    },
    defaults: {
      markdown: true,
      num_history_messages: 20,
      max_tool_calls_from_history: 4,
      compaction: {
        enabled: false,
        reserve_tokens: 16384,
        threshold_percent: 0.8,
        notify: false,
      },
    },
  };

  const mockUpdateTeam = vi.fn();
  const mockDeleteTeam = vi.fn();
  const mockSaveConfig = vi.fn();
  const mockAgentPoliciesByAgent: AgentPoliciesByAgent = {
    code: {
      agent_name: 'code',
      is_private: false,
      effective_execution_scope: null,
      scope_label: 'unscoped',
      scope_source: 'unscoped',
      dashboard_credentials_supported: true,
      team_eligibility_reason: null,
      private_knowledge_base_id: null,
      request_scoped_workspace_enabled: false,
      request_scoped_knowledge_enabled: false,
    },
    shell: {
      agent_name: 'shell',
      is_private: false,
      effective_execution_scope: null,
      scope_label: 'unscoped',
      scope_source: 'unscoped',
      dashboard_credentials_supported: true,
      team_eligibility_reason: null,
      private_knowledge_base_id: null,
      request_scoped_workspace_enabled: false,
      request_scoped_knowledge_enabled: false,
    },
    research: {
      agent_name: 'research',
      is_private: false,
      effective_execution_scope: null,
      scope_label: 'unscoped',
      scope_source: 'unscoped',
      dashboard_credentials_supported: true,
      team_eligibility_reason: null,
      private_knowledge_base_id: null,
      request_scoped_workspace_enabled: false,
      request_scoped_knowledge_enabled: false,
    },
    leader: {
      agent_name: 'leader',
      is_private: false,
      effective_execution_scope: null,
      scope_label: 'unscoped',
      scope_source: 'unscoped',
      dashboard_credentials_supported: true,
      team_eligibility_reason:
        "Delegates to private agent 'mind', so it cannot participate in teams yet.",
      private_knowledge_base_id: null,
      request_scoped_workspace_enabled: false,
      request_scoped_knowledge_enabled: false,
    },
    mind: {
      agent_name: 'mind',
      is_private: true,
      effective_execution_scope: 'user',
      scope_label: 'private.per=user',
      scope_source: 'private.per',
      dashboard_credentials_supported: false,
      team_eligibility_reason: 'Private agents cannot participate in teams yet.',
      private_knowledge_base_id: null,
      request_scoped_workspace_enabled: true,
      request_scoped_knowledge_enabled: false,
    },
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      teams: [mockTeam],
      agents: mockAgents,
      rooms: [
        {
          id: 'dev',
          display_name: 'Dev',
          description: 'Development room',
          agents: ['code', 'shell'],
        },
        { id: 'lobby', display_name: 'Lobby', description: 'Main lobby', agents: [] },
        {
          id: 'research',
          display_name: 'Research',
          description: 'Research room',
          agents: ['research'],
        },
      ],
      selectedTeamId: 'dev_team',
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      agentPoliciesByAgent: mockAgentPoliciesByAgent,
      config: mockConfig,
      isDirty: false,
      diagnostics: [],
      selectTeam: vi.fn(),
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
      rooms: [],
      selectedTeamId: null,
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      agentPoliciesByAgent: {},
      config: mockConfig,
      isDirty: false,
      diagnostics: [],
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

  it('updates team history runs', async () => {
    render(<TeamEditor />);

    const historyRunsInput = screen.getByLabelText('History Runs');
    fireEvent.change(historyRunsInput, { target: { value: '8' } });

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        num_history_runs: 8,
      });
    });
  });

  it('clears invalid negative history runs instead of writing them through', async () => {
    render(<TeamEditor />);

    const historyRunsInput = screen.getByLabelText('History Runs');
    fireEvent.change(historyRunsInput, { target: { value: '-1' } });

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        num_history_runs: null,
      });
    });
  });

  it('clears zero history runs instead of writing them through', async () => {
    render(<TeamEditor />);

    const historyRunsInput = screen.getByLabelText('History Runs');
    fireEvent.change(historyRunsInput, { target: { value: '0' } });

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        num_history_runs: null,
      });
    });
  });

  it('updates team history messages', async () => {
    const messageTeam: Team = {
      ...mockTeam,
      num_history_runs: null,
      num_history_messages: 12,
    };
    (useConfigStore as any).mockReturnValue({
      teams: [messageTeam],
      agents: mockAgents,
      rooms: [
        {
          id: 'dev',
          display_name: 'Dev',
          description: 'Development room',
          agents: ['code', 'shell'],
        },
      ],
      selectedTeamId: 'dev_team',
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      agentPoliciesByAgent: mockAgentPoliciesByAgent,
      config: mockConfig,
      isDirty: false,
      diagnostics: [],
      selectTeam: vi.fn(),
    });

    render(<TeamEditor />);

    const historyMessagesInput = screen.getByLabelText('History Messages');
    fireEvent.change(historyMessagesInput, { target: { value: '15' } });

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        num_history_messages: 15,
      });
    });
  });

  it('clears zero history messages instead of writing them through', async () => {
    const messageTeam: Team = {
      ...mockTeam,
      num_history_runs: null,
      num_history_messages: 12,
    };
    (useConfigStore as any).mockReturnValue({
      teams: [messageTeam],
      agents: mockAgents,
      rooms: [
        {
          id: 'dev',
          display_name: 'Dev',
          description: 'Development room',
          agents: ['code', 'shell'],
        },
      ],
      selectedTeamId: 'dev_team',
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      agentPoliciesByAgent: mockAgentPoliciesByAgent,
      config: mockConfig,
      isDirty: false,
      diagnostics: [],
      selectTeam: vi.fn(),
    });

    render(<TeamEditor />);

    const historyMessagesInput = screen.getByLabelText('History Messages');
    fireEvent.change(historyMessagesInput, { target: { value: '0' } });

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        num_history_messages: null,
      });
    });
  });

  it('updates max tool calls from history', async () => {
    render(<TeamEditor />);

    const maxToolCallsInput = screen.getByLabelText('Max Tool Calls from History');
    fireEvent.change(maxToolCallsInput, { target: { value: '3' } });

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        max_tool_calls_from_history: 3,
      });
    });
  });

  it('authors and clears team compaction overrides', async () => {
    render(<TeamEditor />);

    fireEvent.click(screen.getByRole('checkbox', { name: /enable auto-compaction/i }));

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        compaction: { enabled: true },
      });
    });

    fireEvent.click(screen.getByRole('button', { name: /use inherited settings/i }));

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        compaction: undefined,
      });
    });
  });

  it('normalizes authored team compaction overrides as enabled', () => {
    expect(
      normalizeTeamUpdates(mockTeam, {
        compaction: {
          threshold_percent: 0.6,
          threshold_tokens: null,
        },
      }).compaction
    ).toEqual({
      enabled: true,
      threshold_percent: 0.6,
      threshold_tokens: null,
    });
  });

  it('preserves explicit team compaction model clears during normalization', () => {
    expect(normalizeTeamUpdates(mockTeam, { compaction: { model: null } }).compaction).toEqual({
      enabled: true,
      model: null,
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

  it('disables private agents as team members', () => {
    render(<TeamEditor />);

    const mindCheckbox = screen.getByRole('checkbox', { name: /Mind Agent/i });
    expect(mindCheckbox).toBeDisabled();
    expect(screen.getByText('Private agents cannot participate in teams yet.')).toBeInTheDocument();
  });

  it('disables agents that delegate to private agents', () => {
    render(<TeamEditor />);

    const leaderCheckbox = screen.getByRole('checkbox', { name: /Leader Agent/i });
    expect(leaderCheckbox).toBeDisabled();
    expect(
      screen.getByText("Delegates to private agent 'mind', so it cannot participate in teams yet.")
    ).toBeInTheDocument();
  });

  it('disables agents when policy preview is unavailable', () => {
    (useConfigStore as any).mockReturnValue({
      teams: [mockTeam],
      agents: mockAgents,
      rooms: [
        {
          id: 'dev',
          display_name: 'Dev',
          description: 'Development room',
          agents: ['code', 'shell'],
        },
      ],
      selectedTeamId: 'dev_team',
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      agentPoliciesByAgent: {
        code: mockAgentPoliciesByAgent.code,
        shell: mockAgentPoliciesByAgent.shell,
        leader: mockAgentPoliciesByAgent.leader,
        mind: mockAgentPoliciesByAgent.mind,
      },
      config: mockConfig,
      isDirty: false,
      diagnostics: [],
    });

    render(<TeamEditor />);

    const researchCheckbox = screen.getByRole('checkbox', { name: /Research Agent/i });
    expect(researchCheckbox).toBeDisabled();
    expect(
      screen.getByText(
        'Agent policy preview is unavailable. Save or refresh to validate team eligibility.'
      )
    ).toBeInTheDocument();
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

  it('adds room to team when checkbox is checked', async () => {
    render(<TeamEditor />);

    // Find the research room checkbox specifically (not the research agent checkbox)
    const checkboxes = screen.getAllByRole('checkbox');
    const researchRoomCheckbox = checkboxes.find(cb => cb.id === 'room-research');

    fireEvent.click(researchRoomCheckbox!);

    await waitFor(() => {
      expect(mockUpdateTeam).toHaveBeenCalledWith('dev_team', {
        rooms: ['dev', 'lobby', 'research'],
      });
    });
  });

  it('removes room from team when checkbox is unchecked', async () => {
    render(<TeamEditor />);

    const devCheckbox = screen.getByRole('checkbox', { name: /Dev/ });
    fireEvent.click(devCheckbox);

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
    // Re-mock with isDirty: true so the button is enabled
    (useConfigStore as any).mockReturnValue({
      teams: [mockTeam],
      agents: mockAgents,
      rooms: [
        {
          id: 'dev',
          display_name: 'Dev',
          description: 'Development room',
          agents: ['code', 'shell'],
        },
        { id: 'lobby', display_name: 'Lobby', description: 'Main lobby', agents: [] },
        {
          id: 'research',
          display_name: 'Research',
          description: 'Research room',
          agents: ['research'],
        },
      ],
      selectedTeamId: 'dev_team',
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      agentPoliciesByAgent: mockAgentPoliciesByAgent,
      config: mockConfig,
      isDirty: true,
      diagnostics: [],
    });

    render(<TeamEditor />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).not.toBeDisabled();
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
      rooms: [
        {
          id: 'dev',
          display_name: 'Dev',
          description: 'Development room',
          agents: ['code', 'shell'],
        },
        { id: 'lobby', display_name: 'Lobby', description: 'Main lobby', agents: [] },
      ],
      selectedTeamId: 'dev_team',
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      agentPoliciesByAgent: mockAgentPoliciesByAgent,
      config: mockConfig,
      isDirty: true,
      diagnostics: [],
    });

    render(<TeamEditor />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).not.toBeDisabled();
  });

  it('renders save diagnostics for team validation failures', () => {
    (useConfigStore as any).mockReturnValue({
      teams: [mockTeam],
      agents: mockAgents,
      rooms: [
        {
          id: 'dev',
          display_name: 'Dev',
          description: 'Development room',
          agents: ['code', 'shell'],
        },
      ],
      selectedTeamId: 'dev_team',
      updateTeam: mockUpdateTeam,
      deleteTeam: mockDeleteTeam,
      saveConfig: mockSaveConfig,
      agentPoliciesByAgent: mockAgentPoliciesByAgent,
      config: mockConfig,
      isDirty: true,
      diagnostics: [
        {
          kind: 'validation',
          issue: {
            loc: ['teams', 'dev_team', 'agents'],
            msg: 'Team members cannot include private agents.',
            type: 'value_error',
          },
        },
      ],
    });

    render(<TeamEditor />);

    expect(screen.getByText('Team members cannot include private agents.')).toBeInTheDocument();
  });
});
