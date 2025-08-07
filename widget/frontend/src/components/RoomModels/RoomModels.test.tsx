import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { RoomModels } from './RoomModels';
import { useConfigStore } from '@/store/configStore';
import { Config } from '@/types/config';

// Mock the store
vi.mock('@/store/configStore');

describe('RoomModels', () => {
  const mockConfig: Partial<Config> = {
    room_models: {
      lobby: 'default',
      dev: 'gpt4',
    },
    models: {
      default: { provider: 'ollama', id: 'llama2' },
      gpt4: { provider: 'openai', id: 'gpt-4' },
      claude: { provider: 'anthropic', id: 'claude-3' },
    },
  };

  const mockUpdateRoomModels = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      config: mockConfig,
      updateRoomModels: mockUpdateRoomModels,
    });
  });

  it('renders room models configuration', () => {
    render(<RoomModels />);

    expect(screen.getByText('Room-Specific Models')).toBeInTheDocument();
    expect(screen.getByText(/Configure which models teams should use/)).toBeInTheDocument();
  });

  it('displays existing room model configurations', () => {
    render(<RoomModels />);

    expect(screen.getByText('lobby')).toBeInTheDocument();
    expect(screen.getByText('dev')).toBeInTheDocument();

    // Check that the correct models are selected
    const selectTriggers = screen.getAllByRole('combobox');
    expect(selectTriggers[0]).toHaveTextContent('default');
    expect(selectTriggers[1]).toHaveTextContent('gpt4');
  });

  it('shows empty state when no room models configured', () => {
    (useConfigStore as any).mockReturnValue({
      config: {
        ...mockConfig,
        room_models: {},
      },
      updateRoomModels: mockUpdateRoomModels,
    });

    render(<RoomModels />);

    expect(screen.getByText('No room-specific models configured')).toBeInTheDocument();
    expect(screen.getByText(/Click "Add Room" to configure/)).toBeInTheDocument();
  });

  it('shows add room form when Add Room button is clicked', () => {
    render(<RoomModels />);

    const addButton = screen.getByRole('button', { name: /Add Room/i });
    fireEvent.click(addButton);

    expect(screen.getByLabelText('Room Name')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Enter room name...')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('adds new room with default model', async () => {
    render(<RoomModels />);

    const addButton = screen.getByRole('button', { name: /Add Room/i });
    fireEvent.click(addButton);

    const input = screen.getByPlaceholderText('Enter room name...');
    fireEvent.change(input, { target: { value: 'testing' } });

    const addFormButton = screen.getByRole('button', { name: 'Add' });
    fireEvent.click(addFormButton);

    await waitFor(() => {
      expect(mockUpdateRoomModels).toHaveBeenCalledWith({
        lobby: 'default',
        dev: 'gpt4',
        testing: 'default',
      });
    });
  });

  it('adds room when Enter key is pressed', async () => {
    render(<RoomModels />);

    const addButton = screen.getByRole('button', { name: /Add Room/i });
    fireEvent.click(addButton);

    const input = screen.getByPlaceholderText('Enter room name...');
    fireEvent.change(input, { target: { value: 'new_room' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => {
      expect(mockUpdateRoomModels).toHaveBeenCalledWith({
        lobby: 'default',
        dev: 'gpt4',
        new_room: 'default',
      });
    });
  });

  it('cancels add room when Escape key is pressed', () => {
    render(<RoomModels />);

    const addButton = screen.getByRole('button', { name: /Add Room/i });
    fireEvent.click(addButton);

    const input = screen.getByPlaceholderText('Enter room name...');
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(screen.queryByPlaceholderText('Enter room name...')).not.toBeInTheDocument();
  });

  it('cancels add room when Cancel button is clicked', () => {
    render(<RoomModels />);

    const addButton = screen.getByRole('button', { name: /Add Room/i });
    fireEvent.click(addButton);

    const cancelButton = screen.getByRole('button', { name: 'Cancel' });
    fireEvent.click(cancelButton);

    expect(screen.queryByPlaceholderText('Enter room name...')).not.toBeInTheDocument();
  });

  it('does not add room with empty name', () => {
    render(<RoomModels />);

    const addButton = screen.getByRole('button', { name: /Add Room/i });
    fireEvent.click(addButton);

    const addFormButton = screen.getByRole('button', { name: 'Add' });
    fireEvent.click(addFormButton);

    expect(mockUpdateRoomModels).not.toHaveBeenCalled();
  });

  it('does not add duplicate room', () => {
    render(<RoomModels />);

    const addButton = screen.getByRole('button', { name: /Add Room/i });
    fireEvent.click(addButton);

    const input = screen.getByPlaceholderText('Enter room name...');
    fireEvent.change(input, { target: { value: 'lobby' } }); // Already exists

    const addFormButton = screen.getByRole('button', { name: 'Add' });
    fireEvent.click(addFormButton);

    expect(mockUpdateRoomModels).not.toHaveBeenCalled();
  });

  it('updates room model when selection changes', async () => {
    render(<RoomModels />);

    const selectTriggers = screen.getAllByRole('combobox');
    fireEvent.click(selectTriggers[0]); // Click lobby select

    const claudeOption = await screen.findByText('claude');
    fireEvent.click(claudeOption);

    expect(mockUpdateRoomModels).toHaveBeenCalledWith({
      lobby: 'claude',
      dev: 'gpt4',
    });
  });

  it('removes room when X button is clicked', async () => {
    render(<RoomModels />);

    const removeButtons = screen
      .getAllByRole('button')
      .filter(btn => btn.querySelector('svg')?.classList.contains('h-4'));
    fireEvent.click(removeButtons[0]); // Remove lobby

    await waitFor(() => {
      expect(mockUpdateRoomModels).toHaveBeenCalledWith({
        dev: 'gpt4',
      });
    });
  });

  it('handles empty models config gracefully', () => {
    (useConfigStore as any).mockReturnValue({
      config: {
        room_models: { lobby: 'default' },
        models: {},
      },
      updateRoomModels: mockUpdateRoomModels,
    });

    render(<RoomModels />);

    const selectTrigger = screen.getByRole('combobox');
    fireEvent.click(selectTrigger);

    // Should show no options
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
  });
});
