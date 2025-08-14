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
        name: 'file',
        display_name: 'File',
        setup_type: 'none',
        status: 'available',
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
    instructions: ['Test instruction'],
    rooms: ['test_room'],
    num_history_runs: 5,
  };

  const mockConfig = {
    models: {
      default: { provider: 'test', id: 'test-model' },
      custom: { provider: 'custom', id: 'custom-model' },
    },
    agents: { test_agent: mockAgent },
    defaults: { num_history_runs: 5 },
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

  it('updates tools when checkboxes are toggled', () => {
    render(<AgentEditor />);

    // Find the calculator checkbox (should be checked)
    const calculatorCheckbox = screen.getByRole('checkbox', { name: /calculator/i });
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
    const fileCheckbox = screen.getByRole('checkbox', { name: /file/i });
    fireEvent.click(fileCheckbox);

    expect(mockStore.updateAgent).toHaveBeenCalledWith(
      'test_agent',
      expect.objectContaining({
        tools: ['file'],
      })
    );
  });

  it('handles model selection', () => {
    render(<AgentEditor />);

    // Open the select dropdown
    const modelSelect = screen.getByRole('combobox');
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
