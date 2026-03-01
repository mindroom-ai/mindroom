import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { AgentEditor } from './AgentEditor';
import { useConfigStore } from '@/store/configStore';
import { Agent } from '@/types/config';

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
  };

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue(mockStore);
  });

  it('renders without infinite loops', () => {
    const { container } = render(<AgentEditor />);
    expect(container).toBeTruthy();
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
    // distinguish from the sandbox-tools checkboxes which have "sandbox ..." labels
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

  it('disables memory file path when effective backend is not file', () => {
    render(<AgentEditor />);
    expect(screen.getByLabelText('Memory File Path')).toBeDisabled();
  });

  it('updates memory file path when changed', () => {
    render(<AgentEditor />);

    const memoryBackendSelect = screen.getByLabelText('Memory Backend');
    fireEvent.click(memoryBackendSelect);
    const fileOption = screen.getByRole('option', { name: 'File (markdown memory)' });
    fireEvent.click(fileOption);

    const memoryPathInput = screen.getByLabelText('Memory File Path');
    expect(memoryPathInput).not.toBeDisabled();
    fireEvent.change(memoryPathInput, { target: { value: './openclaw_data' } });

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        memory_file_path: './openclaw_data',
      })
    );
  });

  it('clears memory file path when input is emptied', () => {
    (useConfigStore as any).mockReturnValue({
      ...mockStore,
      agents: [{ ...mockAgent, memory_backend: 'file', memory_file_path: './openclaw_data' }],
      rooms: mockStore.rooms,
    });

    render(<AgentEditor />);

    const memoryPathInput = screen.getByLabelText('Memory File Path');
    expect(memoryPathInput).not.toBeDisabled();
    fireEvent.change(memoryPathInput, { target: { value: '' } });

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        memory_file_path: undefined,
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

  describe('sandbox_tools inheritance', () => {
    const twoToolAgent = { ...mockAgent, tools: ['calculator', 'file'] };

    it('shows inherited defaults as checked with (default) label', () => {
      (useConfigStore as any).mockReturnValue({
        ...mockStore,
        agents: [{ ...twoToolAgent, sandbox_tools: undefined }],
        config: {
          ...mockConfig,
          defaults: { ...mockConfig.defaults, sandbox_tools: ['calculator'] },
        },
        rooms: mockStore.rooms,
      });

      render(<AgentEditor />);

      const sandboxCalc = screen.getByRole('checkbox', { name: 'sandbox calculator' });
      expect(sandboxCalc).toBeChecked();
      expect(screen.getByText('calculator (default)')).toBeTruthy();

      const sandboxFile = screen.getByRole('checkbox', { name: 'sandbox file' });
      expect(sandboxFile).not.toBeChecked();
    });

    it('seeds from defaults on first toggle so other defaults are preserved', () => {
      (useConfigStore as any).mockReturnValue({
        ...mockStore,
        agents: [{ ...twoToolAgent, sandbox_tools: undefined }],
        config: {
          ...mockConfig,
          defaults: { ...mockConfig.defaults, sandbox_tools: ['calculator'] },
        },
        rooms: mockStore.rooms,
      });

      render(<AgentEditor />);

      // Toggle file ON — should seed from defaults first, so calculator stays
      const sandboxFile = screen.getByRole('checkbox', { name: 'sandbox file' });
      fireEvent.click(sandboxFile);

      expect(mockStore.updateAgent).toHaveBeenCalledWith(
        'test_agent',
        expect.objectContaining({
          sandbox_tools: ['calculator', 'file'],
        })
      );
    });

    it('renders empty list as explicit disable (all unchecked, no default labels)', () => {
      (useConfigStore as any).mockReturnValue({
        ...mockStore,
        agents: [{ ...twoToolAgent, sandbox_tools: [] }],
        config: {
          ...mockConfig,
          defaults: { ...mockConfig.defaults, sandbox_tools: ['calculator'] },
        },
        rooms: mockStore.rooms,
      });

      render(<AgentEditor />);

      const sandboxCalc = screen.getByRole('checkbox', { name: 'sandbox calculator' });
      expect(sandboxCalc).not.toBeChecked();

      const sandboxFile = screen.getByRole('checkbox', { name: 'sandbox file' });
      expect(sandboxFile).not.toBeChecked();

      // No sandbox tool label should show "(default)"
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
