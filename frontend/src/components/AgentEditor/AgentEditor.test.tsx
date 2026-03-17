import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { AgentEditor } from './AgentEditor';
import { useConfigStore } from '@/store/configStore';
import { Agent, SHARED_CONTEXT_FILE_PLACEHOLDER } from '@/types/config';
import { useTools } from '@/hooks/useTools';

// Mock the store
vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

// Mock useTools hook
vi.mock('@/hooks/useTools', () => ({
  useTools: vi.fn(() => ({
    tools: [
      {
        name: 'calculator',
        display_name: 'Calculator',
        setup_type: 'none',
        status: 'available',
      },
      {
        name: 'delegate',
        display_name: 'Agent Delegation',
        setup_type: 'none',
        status: 'available',
      },
      {
        name: 'file',
        display_name: 'File',
        setup_type: 'none',
        status: 'available',
      },
    ],
    loading: false,
  })),
}));

vi.mock('@/hooks/useSkills', () => ({
  useSkills: vi.fn(() => ({
    skills: [
      {
        name: 'debugging',
        description: 'Debug issues quickly',
        origin: 'bundled',
        can_edit: false,
      },
      {
        name: 'code-review',
        description: 'Perform code reviews',
        origin: 'user',
        can_edit: true,
      },
    ],
    loading: false,
  })),
}));

describe('AgentEditor', () => {
  const mockAgent: Agent = {
    id: 'test_agent',
    display_name: 'Test Agent',
    role: 'Test role',
    tools: ['calculator'],
    skills: ['debugging'],
    instructions: ['Test instruction'],
    rooms: ['test_room'],
    knowledge_bases: ['research'],
    learning: true,
    learning_mode: 'always',
  };

  const mockConfig = {
    models: {
      default: { provider: 'test', id: 'test-model' },
      custom: { provider: 'custom', id: 'custom-model' },
    },
    memory: {
      backend: 'mem0',
      embedder: {
        provider: 'openai',
        config: { model: 'text-embedding-3-small' },
      },
    },
    agents: { test_agent: mockAgent },
    knowledge_bases: {
      legal: { path: './legal', watch: true },
      research: { path: './research', watch: true },
    },
    defaults: {},
  };

  const mockStore = {
    agents: [mockAgent],
    rooms: [
      {
        id: 'test_room',
        display_name: 'Test Room',
        description: 'Test room',
        agents: ['test_agent'],
      },
      { id: 'other_room', display_name: 'Other Room', description: 'Another room', agents: [] },
    ],
    selectedAgentId: 'test_agent',
    updateAgent: vi.fn(),
    deleteAgent: vi.fn(),
    saveConfig: vi.fn().mockResolvedValue(undefined),
    config: mockConfig,
    isDirty: false,
    diagnostics: [],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue(mockStore);
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: 'calculator',
          display_name: 'Calculator',
          setup_type: 'none',
          status: 'available',
        },
        {
          name: 'delegate',
          display_name: 'Agent Delegation',
          setup_type: 'none',
          status: 'available',
        },
        {
          name: 'file',
          display_name: 'File',
          setup_type: 'none',
          status: 'available',
        },
      ],
      loading: false,
    });
  });

  it('renders without infinite loops', () => {
    const { container } = render(<AgentEditor />);
    expect(container).toBeTruthy();
  });

  it('requests agent-scoped tools for the selected agent', () => {
    render(<AgentEditor />);

    expect(useTools).toHaveBeenCalledWith('test_agent', null);
  });

  it('shows selectable setup-required tools instead of hiding them', () => {
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: 'calculator',
          display_name: 'Calculator',
          setup_type: 'none',
          status: 'available',
        },
        {
          name: 'weather',
          display_name: 'Weather',
          setup_type: 'api_key',
          status: 'requires_config',
          dashboard_configuration_supported: true,
        },
      ],
      loading: false,
    });

    render(<AgentEditor />);

    expect(screen.getByText('Setup Required')).toBeInTheDocument();
    expect(screen.getByLabelText('Weather')).toBeInTheDocument();
  });

  it('keeps selected unsupported tools visible so they can be removed', async () => {
    const invalidToolAgent = { ...mockAgent, tools: ['gmail'] };
    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [invalidToolAgent],
      config: {
        ...mockConfig,
        agents: { test_agent: invalidToolAgent },
      },
    });
    (useTools as any).mockReturnValue({
      tools: [
        {
          name: 'gmail',
          display_name: 'Gmail',
          setup_type: 'oauth',
          status: 'requires_config',
          execution_scope_supported: false,
        },
      ],
      loading: false,
    });

    render(<AgentEditor />);

    await waitFor(() => {
      expect(screen.getByText('Selected But Unavailable')).toBeInTheDocument();
      expect(screen.getByLabelText('Gmail')).toBeInTheDocument();
      expect(
        screen.getByText('Not supported for this execution scope. Uncheck it to remove it.')
      ).toBeInTheDocument();
    });
  });

  it('displays selected agent details', () => {
    render(<AgentEditor />);

    expect(screen.getByDisplayValue('Test Agent')).toBeTruthy();
    expect(screen.getByDisplayValue('Test role')).toBeTruthy();
    expect(screen.getByDisplayValue('Test instruction')).toBeTruthy();
    // Rooms are now displayed as checkboxes, not input fields
    const testRoomCheckbox = screen.getByRole('checkbox', { name: /Test Room/i });
    expect(testRoomCheckbox).toBeChecked();
  });

  it('shows empty state when no agent is selected', () => {
    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      selectedAgentId: null,
      rooms: mockStore.rooms,
    });

    render(<AgentEditor />);
    expect(screen.getByText('Select an agent to edit')).toBeTruthy();
  });

  it('calls updateAgent when form fields change', async () => {
    render(<AgentEditor />);

    const displayNameInput = screen.getByLabelText('Display Name');
    fireEvent.change(displayNameInput, { target: { value: 'Updated Agent' } });

    // Wait a bit to ensure the update is called
    await waitFor(() => {
      expect(mockStore.updateAgent).toHaveBeenCalled();
    });
  });

  it('does not cause infinite update loops when updateAgent is called', async () => {
    let updateCount = 0;
    const trackingUpdateAgent = vi.fn((_id, _updates) => {
      updateCount++;
      // Simulate what the real updateAgent does - updates the agent in the store
      mockStore.agents = mockStore.agents.map(agent =>
        agent.id === _id ? { ...agent, ..._updates } : agent
      );
    });

    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      updateAgent: trackingUpdateAgent,
      rooms: mockStore.rooms,
    });

    render(<AgentEditor />);

    const displayNameInput = screen.getByLabelText('Display Name');
    fireEvent.change(displayNameInput, { target: { value: 'Updated Agent' } });

    // Wait to see if multiple updates occur
    await waitFor(() => {
      expect(updateCount).toBeGreaterThan(0);
    });

    // The update count should be reasonable (not hundreds/thousands)
    expect(updateCount).toBeLessThan(10);
  });

  it('handles save button click', async () => {
    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      isDirty: true,
      rooms: mockStore.rooms,
    });

    render(<AgentEditor />);

    const saveButton = screen.getByRole('button', { name: /save/i });
    expect(saveButton).not.toBeDisabled();

    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockStore.saveConfig).toHaveBeenCalled();
    });
  });

  it('renders backend validation errors for private fields', () => {
    const privateAgent: Agent = {
      ...mockAgent,
      private: {
        per: 'user',
        root: '../outside',
        template_dir: '   ',
        context_files: ['../SOUL.md'],
        knowledge: {
          enabled: true,
          path: '../memory',
          watch: true,
        },
      },
    };

    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [privateAgent],
      config: {
        ...mockConfig,
        agents: {
          test_agent: privateAgent,
        },
      },
      diagnostics: [
        {
          kind: 'global',
          message: 'Configuration validation failed',
          blocking: false,
        },
        {
          kind: 'validation',
          issue: {
            loc: ['agents', 'test_agent', 'private', 'root'],
            msg: 'private.root must stay within the private instance root',
            type: 'value_error',
          },
        },
        {
          kind: 'validation',
          issue: {
            loc: ['agents', 'test_agent', 'private', 'template_dir'],
            msg: 'template_dir must not be blank',
            type: 'value_error',
          },
        },
        {
          kind: 'validation',
          issue: {
            loc: ['agents', 'test_agent', 'private', 'context_files'],
            msg: 'private.context_files must stay under the private root',
            type: 'value_error',
          },
        },
        {
          kind: 'validation',
          issue: {
            loc: ['agents', 'test_agent', 'private', 'knowledge', 'path'],
            msg: 'private.knowledge.path must stay under the private root',
            type: 'value_error',
          },
        },
      ],
    });

    render(<AgentEditor />);

    expect(
      screen.getByText('private.root must stay within the private instance root')
    ).toBeInTheDocument();
    expect(screen.getByText('template_dir must not be blank')).toBeInTheDocument();
    expect(
      screen.getByText('private.context_files must stay under the private root')
    ).toBeInTheDocument();
    expect(
      screen.getByText('private.knowledge.path must stay under the private root')
    ).toBeInTheDocument();
  });

  it('disables save button when not dirty', () => {
    render(<AgentEditor />);

    const saveButton = screen.getByRole('button', { name: /save/i });
    expect(saveButton).toBeDisabled();
  });

  it('handles delete button click with confirmation', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<AgentEditor />);

    const deleteButton = screen.getByRole('button', { name: /delete/i });
    fireEvent.click(deleteButton);

    expect(confirmSpy).toHaveBeenCalledWith('Are you sure you want to delete this agent?');
    expect(mockStore.deleteAgent).toHaveBeenCalledWith('test_agent');

    confirmSpy.mockRestore();
  });

  it('does not delete when user cancels confirmation', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

    render(<AgentEditor />);

    const deleteButton = screen.getByRole('button', { name: /delete/i });
    fireEvent.click(deleteButton);

    expect(mockStore.deleteAgent).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  it('adds and removes instructions', () => {
    render(<AgentEditor />);

    // Find add instruction button
    const addInstructionButton = screen.getByTestId('add-instruction-button');

    fireEvent.click(addInstructionButton);

    // Should have called updateAgent with new instruction
    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        instructions: ['Test instruction', ''],
      })
    );
  });

  it('adds and removes rooms', () => {
    render(<AgentEditor />);

    // Test Room checkbox should be checked initially
    const testRoomCheckbox = screen.getByRole('checkbox', { name: /Test Room/i });
    expect(testRoomCheckbox).toBeChecked();

    // Uncheck Test Room
    fireEvent.click(testRoomCheckbox);
    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        rooms: [],
      })
    );

    // Check Other Room
    const otherRoomCheckbox = screen.getByRole('checkbox', { name: /Other Room/i });
    fireEvent.click(otherRoomCheckbox);
    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        rooms: ['other_room'],
      })
    );
  });

  it('enables requester-private state with the default private scope', async () => {
    render(<AgentEditor />);

    fireEvent.click(screen.getByLabelText('Enable requester-private state'));

    await waitFor(() => {
      expect(mockStore.updateAgent).toHaveBeenCalledWith(
        'test_agent',
        expect.objectContaining({
          private: { per: 'user' },
        })
      );
    });
  });

  it('clears explicit worker_scope when enabling private state', async () => {
    const scopedAgent: Agent = {
      ...mockAgent,
      worker_scope: 'user_agent',
    };

    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [scopedAgent],
      config: {
        ...mockConfig,
        agents: {
          test_agent: scopedAgent,
        },
      },
    });

    render(<AgentEditor />);

    fireEvent.click(screen.getByLabelText('Enable requester-private state'));

    await waitFor(() => {
      expect(mockStore.updateAgent).toHaveBeenCalledWith(
        'test_agent',
        expect.objectContaining({
          worker_scope: undefined,
          private: { per: 'user' },
        })
      );
    });
  });

  it('does not restore prior worker_scope when private mode is disabled again', async () => {
    const scopedAgent: Agent = {
      ...mockAgent,
      worker_scope: 'user_agent',
    };

    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [scopedAgent],
      config: {
        ...mockConfig,
        agents: {
          test_agent: scopedAgent,
        },
      },
    });

    render(<AgentEditor />);

    const privateToggle = screen.getByLabelText('Enable requester-private state');
    fireEvent.click(privateToggle);
    fireEvent.click(privateToggle);

    await waitFor(() => {
      expect(mockStore.updateAgent).toHaveBeenNthCalledWith(
        1,
        'test_agent',
        expect.objectContaining({
          worker_scope: undefined,
          private: { per: 'user' },
        })
      );
      expect(mockStore.updateAgent).toHaveBeenNthCalledWith(
        2,
        'test_agent',
        expect.objectContaining({
          private: undefined,
        })
      );
    });

    expect(mockStore.updateAgent.mock.calls[1][1]).not.toHaveProperty('worker_scope');
  });

  it('renders and updates private agent fields', async () => {
    const privateAgent: Agent = {
      ...mockAgent,
      private: {
        per: 'user_agent',
        root: 'mind_data',
        template_dir: './mind_template',
        context_files: ['SOUL.md'],
        knowledge: {
          enabled: true,
          path: 'memory',
          watch: true,
        },
      },
    };

    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [privateAgent],
      config: {
        ...mockConfig,
        agents: {
          test_agent: privateAgent,
        },
      },
    });

    render(<AgentEditor />);

    expect(screen.getByDisplayValue('mind_data')).toBeInTheDocument();
    expect(screen.getByDisplayValue('./mind_template')).toBeInTheDocument();
    expect(screen.getByDisplayValue('SOUL.md')).toBeInTheDocument();
    expect(screen.getByDisplayValue('memory')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Private Root'), {
      target: { value: 'updated_data' },
    });

    await waitFor(() => {
      expect(mockStore.updateAgent).toHaveBeenCalledWith(
        'test_agent',
        expect.objectContaining({
          private: expect.objectContaining({
            per: 'user_agent',
            root: 'updated_data',
          }),
        })
      );
    });
  });

  it('enables private knowledge with a default path', async () => {
    render(<AgentEditor />);

    fireEvent.click(screen.getByLabelText('Enable requester-private state'));
    fireEvent.click(screen.getByLabelText('Enable private knowledge'));

    await waitFor(() => {
      expect(mockStore.updateAgent).toHaveBeenCalledWith(
        'test_agent',
        expect.objectContaining({
          private: expect.objectContaining({
            knowledge: expect.objectContaining({
              enabled: true,
              path: 'memory',
              watch: true,
            }),
          }),
        })
      );
    });
  });

  it('uses the canonical shared context placeholder', async () => {
    const agentWithoutContextFiles: Agent = {
      ...mockAgent,
      context_files: [],
    };

    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [agentWithoutContextFiles],
      config: {
        ...mockConfig,
        agents: {
          test_agent: agentWithoutContextFiles,
        },
      },
    });

    render(<AgentEditor />);

    fireEvent.click(screen.getByTestId('add-context-file-button'));

    expect(screen.getByPlaceholderText(SHARED_CONTEXT_FILE_PLACEHOLDER)).toBeInTheDocument();
  });

  it('updates knowledge bases when checkboxes are toggled', () => {
    render(<AgentEditor />);

    const researchCheckbox = screen.getByRole('checkbox', { name: /research/i });
    expect(researchCheckbox).toBeChecked();

    const legalCheckbox = screen.getByRole('checkbox', { name: /legal/i });
    fireEvent.click(legalCheckbox);
    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        knowledge_bases: ['research', 'legal'],
      })
    );

    fireEvent.click(researchCheckbox);
    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        knowledge_bases: ['legal'],
      })
    );
  });

  it('hides delegate from the tools picker', () => {
    render(<AgentEditor />);

    // calculator and file should appear as checkboxes
    expect(screen.getByRole('checkbox', { name: 'Calculator' })).toBeTruthy();
    expect(screen.getByRole('checkbox', { name: 'File' })).toBeTruthy();

    // delegate should NOT appear even though useTools returns it
    expect(screen.queryByRole('checkbox', { name: /agent delegation/i })).toBeNull();
  });

  it('updates tools when checkboxes are toggled', () => {
    render(<AgentEditor />);

    // Find the calculator checkbox (should be checked) — use exact name to
    // distinguish from the worker-tools checkboxes which have "worker ..." labels
    const calculatorCheckbox = screen.getByRole('checkbox', { name: 'Calculator' });
    expect(calculatorCheckbox).toBeChecked();

    // Uncheck it
    fireEvent.click(calculatorCheckbox);

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        tools: [],
      })
    );

    // Check another tool
    const fileCheckbox = screen.getByRole('checkbox', { name: 'File' });
    fireEvent.click(fileCheckbox);

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        tools: ['file'],
      })
    );
  });

  it('updates skills when checkboxes are toggled', async () => {
    render(<AgentEditor />);

    const debuggingCheckbox = await screen.findByRole('checkbox', { name: /debugging/i });
    expect(debuggingCheckbox).toBeChecked();

    fireEvent.click(debuggingCheckbox);
    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        skills: [],
      })
    );

    const codeReviewCheckbox = screen.getByRole('checkbox', { name: /code-review/i });
    fireEvent.click(codeReviewCheckbox);
    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        skills: ['code-review'],
      })
    );
  });

  it('renders missing assigned skills so they can be removed', () => {
    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [
        {
          ...mockAgent,
          skills: ['ghost-skill'],
        },
      ],
      rooms: mockStore.rooms,
    });

    render(<AgentEditor />);

    const ghostSkillCheckbox = screen.getByRole('checkbox', { name: /ghost-skill/i });
    expect(ghostSkillCheckbox).toBeChecked();

    fireEvent.click(ghostSkillCheckbox);
    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        skills: [],
      })
    );
  });

  it('handles model selection', () => {
    render(<AgentEditor />);

    // Open the select dropdown
    const modelSelect = screen.getByLabelText('Model');
    fireEvent.click(modelSelect);

    // Select a different model
    const customOption = screen.getByRole('option', { name: 'custom' });
    fireEvent.click(customOption);

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        model: 'custom',
      })
    );
  });

  it('updates memory backend when selected', () => {
    render(<AgentEditor />);

    const memoryBackendSelect = screen.getByLabelText('Memory Backend');
    fireEvent.click(memoryBackendSelect);

    const fileOption = screen.getByRole('option', { name: 'File (markdown memory)' });
    fireEvent.click(fileOption);

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        memory_backend: 'file',
      })
    );
  });

  it('clears memory backend override when inherit is selected', () => {
    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [{ ...mockAgent, memory_backend: 'file' }],
      rooms: mockStore.rooms,
    });

    render(<AgentEditor />);

    const memoryBackendSelect = screen.getByLabelText('Memory Backend');
    fireEvent.click(memoryBackendSelect);

    const inheritOption = screen.getByRole('option', { name: /Inherit global/i });
    fireEvent.click(inheritOption);

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        memory_backend: undefined,
      })
    );
  });

  it('updates learning mode when selected', () => {
    render(<AgentEditor />);

    const modeSelect = screen.getByLabelText('Learning Mode');
    fireEvent.click(modeSelect);

    const agenticOption = screen.getByRole('option', { name: 'Agentic (tool-driven)' });
    fireEvent.click(agenticOption);

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        learning_mode: 'agentic',
      })
    );
  });

  it('uses config defaults when agent learning fields are omitted', () => {
    const agentWithoutLearning = {
      ...mockAgent,
      learning: undefined,
      learning_mode: undefined,
    };
    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [agentWithoutLearning],
      config: {
        ...mockConfig,
        defaults: {
          ...mockConfig.defaults,
          learning: false,
          learning_mode: 'agentic',
        },
      },
      rooms: mockStore.rooms,
    });

    render(<AgentEditor />);

    const learningCheckbox = screen.getByRole('checkbox', { name: /enable learning/i });
    expect(learningCheckbox).not.toBeChecked();
    expect(screen.getByLabelText('Learning Mode')).toHaveTextContent('Agentic (tool-driven)');
  });

  it('updates learning when checkbox is toggled', () => {
    render(<AgentEditor />);

    const learningCheckbox = screen.getByRole('checkbox', { name: /enable learning/i });
    expect(learningCheckbox).toBeChecked();

    fireEvent.click(learningCheckbox);

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        learning: false,
      })
    );
  });

  describe('worker_tools inheritance', () => {
    const twoToolAgent = { ...mockAgent, tools: ['calculator', 'file'] };

    it('shows inherited defaults as checked with (default) label', () => {
      (useConfigStore as any).mockReturnValue({
        ...mockStore,
        agents: [{ ...twoToolAgent, worker_tools: undefined }],
        config: {
          ...mockConfig,
          defaults: { ...mockConfig.defaults, worker_tools: ['calculator'] },
        },
        rooms: mockStore.rooms,
      });

      render(<AgentEditor />);

      const workerCalc = screen.getByRole('checkbox', { name: 'worker calculator' });
      expect(workerCalc).toBeChecked();
      expect(screen.getByText('calculator (default)')).toBeTruthy();

      const workerFile = screen.getByRole('checkbox', { name: 'worker file' });
      expect(workerFile).not.toBeChecked();
    });

    it('seeds from defaults on first toggle so other defaults are preserved', () => {
      (useConfigStore as any).mockReturnValue({
        ...mockStore,
        agents: [{ ...twoToolAgent, worker_tools: undefined }],
        config: {
          ...mockConfig,
          defaults: { ...mockConfig.defaults, worker_tools: ['calculator'] },
        },
        rooms: mockStore.rooms,
      });

      render(<AgentEditor />);

      // Toggle file ON — should seed from defaults first, so calculator stays
      const workerFile = screen.getByRole('checkbox', { name: 'worker file' });
      fireEvent.click(workerFile);

      expect(mockStore.updateAgent).toHaveBeenCalledWith(
        'test_agent',
        expect.objectContaining({
          worker_tools: ['calculator', 'file'],
        })
      );
    });

    it('renders empty list as explicit disable (all unchecked, no default labels)', () => {
      (useConfigStore as any).mockReturnValue({
        ...mockStore,
        agents: [{ ...twoToolAgent, worker_tools: [] }],
        config: {
          ...mockConfig,
          defaults: { ...mockConfig.defaults, worker_tools: ['calculator'] },
        },
        rooms: mockStore.rooms,
      });

      render(<AgentEditor />);

      const workerCalc = screen.getByRole('checkbox', { name: 'worker calculator' });
      expect(workerCalc).not.toBeChecked();

      const workerFile = screen.getByRole('checkbox', { name: 'worker file' });
      expect(workerFile).not.toBeChecked();

      // No worker tool label should show "(default)"
      expect(screen.queryByText('calculator (default)')).toBeNull();
      expect(screen.queryByText('file (default)')).toBeNull();
    });
  });

  it('regression test: form updates should not cause infinite loops', async () => {
    let updateCount = 0;
    const trackingUpdateAgent = vi.fn((_id, _updates) => {
      updateCount++;
    });

    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      updateAgent: trackingUpdateAgent,
      rooms: mockStore.rooms,
    });

    render(<AgentEditor />);

    // Simulate typing in the display name field
    const displayNameInput = screen.getByLabelText('Display Name');

    // Type several characters
    fireEvent.change(displayNameInput, { target: { value: 'U' } });
    fireEvent.change(displayNameInput, { target: { value: 'Up' } });
    fireEvent.change(displayNameInput, { target: { value: 'Updated' } });

    // Wait a bit to ensure any potential loops would have time to manifest
    await new Promise(resolve => setTimeout(resolve, 100));

    // Each change should result in exactly one update call
    expect(updateCount).toBe(3);

    // Now test that rapid changes don't cause exponential updates
    updateCount = 0;
    for (let i = 0; i < 10; i++) {
      fireEvent.change(displayNameInput, { target: { value: `Updated ${i}` } });
    }

    await new Promise(resolve => setTimeout(resolve, 100));

    // Should be exactly 10 updates, not hundreds or thousands
    expect(updateCount).toBe(10);
  });
});
