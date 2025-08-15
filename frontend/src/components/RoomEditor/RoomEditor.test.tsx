import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { RoomEditor } from './RoomEditor';
import { useConfigStore } from '@/store/configStore';
import { Room, Agent, Config } from '@/types/config';

// Mock the store
vi.mock('@/store/configStore');

describe('RoomEditor', () => {
  const mockRoom: Room = {
    id: 'lobby',
    display_name: 'Lobby',
    description: 'Main discussion room',
    agents: ['agent1'],
    model: 'default',
  };

  const mockAgents: Agent[] = [
    {
      id: 'agent1',
      display_name: 'Agent 1',
      role: 'Assistant',
      tools: ['file'],
      instructions: [],
      rooms: ['lobby'],
      num_history_runs: 5,
    },
    {
      id: 'agent2',
      display_name: 'Agent 2',
      role: 'Helper',
      tools: ['shell'],
      instructions: [],
      rooms: [],
      num_history_runs: 5,
    },
  ];

  const mockConfig: Partial<Config> = {
    models: {
      default: { provider: 'ollama', id: 'llama2' },
      gpt4: { provider: 'openai', id: 'gpt-4' },
    },
  };

  const mockUpdateRoom = vi.fn();
  const mockDeleteRoom = vi.fn();
  const mockSaveConfig = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      rooms: [mockRoom],
      agents: mockAgents,
      config: mockConfig,
      selectedRoomId: 'lobby',
      updateRoom: mockUpdateRoom,
      deleteRoom: mockDeleteRoom,
      saveConfig: mockSaveConfig,
      isDirty: false,
    });
  });

  it('renders room editor with room details', () => {
    render(<RoomEditor />);

    const displayNameInput = screen.getByDisplayValue('Lobby');
    expect(displayNameInput).toBeInTheDocument();

    const descriptionInput = screen.getByDisplayValue('Main discussion room');
    expect(descriptionInput).toBeInTheDocument();

    expect(screen.getByText('Room Details')).toBeInTheDocument();
  });

  it('shows placeholder when no room is selected', () => {
    (useConfigStore as any).mockReturnValue({
      rooms: [],
      agents: mockAgents,
      config: mockConfig,
      selectedRoomId: null,
      updateRoom: mockUpdateRoom,
      deleteRoom: mockDeleteRoom,
      saveConfig: mockSaveConfig,
      isDirty: false,
    });

    render(<RoomEditor />);

    expect(screen.getByText('Select a room to edit')).toBeInTheDocument();
  });

  it('updates room display name', async () => {
    render(<RoomEditor />);

    const nameInput = screen.getByLabelText('Display Name');
    fireEvent.change(nameInput, { target: { value: 'Updated Room Name' } });

    await waitFor(() => {
      expect(mockUpdateRoom).toHaveBeenCalledWith('lobby', {
        display_name: 'Updated Room Name',
      });
    });
  });

  it('updates room description', async () => {
    render(<RoomEditor />);

    const descriptionInput = screen.getByLabelText('Description');
    fireEvent.change(descriptionInput, { target: { value: 'Updated description' } });

    await waitFor(() => {
      expect(mockUpdateRoom).toHaveBeenCalledWith('lobby', {
        description: 'Updated description',
      });
    });
  });

  it('displays agents with checkboxes', () => {
    render(<RoomEditor />);

    expect(screen.getByText('Agent 1')).toBeInTheDocument();
    expect(screen.getByText('Agent 2')).toBeInTheDocument();
    expect(screen.getByText('Assistant')).toBeInTheDocument();
    expect(screen.getByText('Helper')).toBeInTheDocument();

    // Agent 1 should be checked (in the room)
    const agent1Checkbox = screen.getByRole('checkbox', { name: /Agent 1/ });
    expect(agent1Checkbox).toBeChecked();

    // Agent 2 should not be checked
    const agent2Checkbox = screen.getByRole('checkbox', { name: /Agent 2/ });
    expect(agent2Checkbox).not.toBeChecked();
  });

  it('adds agent to room when checkbox is checked', async () => {
    render(<RoomEditor />);

    const agent2Checkbox = screen.getByRole('checkbox', { name: /Agent 2/ });
    fireEvent.click(agent2Checkbox);

    await waitFor(() => {
      expect(mockUpdateRoom).toHaveBeenCalledWith('lobby', {
        agents: ['agent1', 'agent2'],
      });
    });
  });

  it('removes agent from room when checkbox is unchecked', async () => {
    render(<RoomEditor />);

    const agent1Checkbox = screen.getByRole('checkbox', { name: /Agent 1/ });
    fireEvent.click(agent1Checkbox);

    await waitFor(() => {
      expect(mockUpdateRoom).toHaveBeenCalledWith('lobby', {
        agents: [],
      });
    });
  });

  it('changes room model', async () => {
    render(<RoomEditor />);

    const modelSelect = screen.getByLabelText('Room Model (Optional)');
    fireEvent.click(modelSelect);

    const gpt4Option = await screen.findByText('gpt4');
    fireEvent.click(gpt4Option);

    expect(mockUpdateRoom).toHaveBeenCalledWith('lobby', {
      model: 'gpt4',
    });
  });

  it('sets model to undefined when default is selected', async () => {
    render(<RoomEditor />);

    const modelSelect = screen.getByLabelText('Room Model (Optional)');
    fireEvent.click(modelSelect);

    const defaultOption = await screen.findByText('Use default model');
    fireEvent.click(defaultOption);

    expect(mockUpdateRoom).toHaveBeenCalledWith('lobby', {
      model: undefined,
    });
  });

  it('calls deleteRoom when delete button is clicked', async () => {
    window.confirm = vi.fn(() => true);
    render(<RoomEditor />);

    const deleteButton = screen.getByRole('button', { name: /Delete/i });
    fireEvent.click(deleteButton);

    expect(window.confirm).toHaveBeenCalledWith('Are you sure you want to delete this room?');
    expect(mockDeleteRoom).toHaveBeenCalledWith('lobby');
  });

  it('does not delete room when confirm is cancelled', () => {
    window.confirm = vi.fn(() => false);
    render(<RoomEditor />);

    const deleteButton = screen.getByRole('button', { name: /Delete/i });
    fireEvent.click(deleteButton);

    expect(mockDeleteRoom).not.toHaveBeenCalled();
  });

  it('calls saveConfig when save button is clicked', async () => {
    // Re-mock with isDirty: true so the button is enabled
    (useConfigStore as any).mockReturnValue({
      rooms: [mockRoom],
      agents: mockAgents,
      config: mockConfig,
      selectedRoomId: 'lobby',
      updateRoom: mockUpdateRoom,
      deleteRoom: mockDeleteRoom,
      saveConfig: mockSaveConfig,
      isDirty: true,
    });

    render(<RoomEditor />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).not.toBeDisabled();
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockSaveConfig).toHaveBeenCalled();
    });
  });

  it('disables save button when not dirty', () => {
    render(<RoomEditor />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).toBeDisabled();
  });

  it('enables save button when dirty', () => {
    (useConfigStore as any).mockReturnValue({
      rooms: [mockRoom],
      agents: mockAgents,
      config: mockConfig,
      selectedRoomId: 'lobby',
      updateRoom: mockUpdateRoom,
      deleteRoom: mockDeleteRoom,
      saveConfig: mockSaveConfig,
      isDirty: true,
    });

    render(<RoomEditor />);

    const saveButton = screen.getByRole('button', { name: /Save/i });
    expect(saveButton).not.toBeDisabled();
  });

  it('shows empty state when no agents available', () => {
    (useConfigStore as any).mockReturnValue({
      rooms: [mockRoom],
      agents: [],
      config: mockConfig,
      selectedRoomId: 'lobby',
      updateRoom: mockUpdateRoom,
      deleteRoom: mockDeleteRoom,
      saveConfig: mockSaveConfig,
      isDirty: false,
    });

    render(<RoomEditor />);

    expect(screen.getByText('No agents available')).toBeInTheDocument();
  });

  it('shows help text for agent selection', () => {
    render(<RoomEditor />);

    expect(
      screen.getByText(/Select agents that should have access to this room/)
    ).toBeInTheDocument();
  });
});
