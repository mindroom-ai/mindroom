import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { RoomList } from './RoomList';
import { useConfigStore } from '@/store/configStore';
import { Room } from '@/types/config';

// Mock the store
vi.mock('@/store/configStore');

describe('RoomList', () => {
  const mockRooms: Room[] = [
    {
      id: 'lobby',
      display_name: 'Lobby',
      description: 'Main discussion room',
      agents: ['agent1', 'agent2'],
      model: 'default',
    },
    {
      id: 'dev_room',
      display_name: 'Dev Room',
      description: 'Development discussions',
      agents: ['agent1'],
    },
  ];

  const mockSelectRoom = vi.fn();
  const mockCreateRoom = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      rooms: mockRooms,
      selectedRoomId: null,
      selectRoom: mockSelectRoom,
      createRoom: mockCreateRoom,
    });
  });

  it('renders room list correctly', () => {
    render(<RoomList />);

    expect(screen.getByText('Rooms')).toBeInTheDocument();
    expect(screen.getByText('Lobby')).toBeInTheDocument();
    expect(screen.getByText('Dev Room')).toBeInTheDocument();
    expect(screen.getByText('Main discussion room')).toBeInTheDocument();
    expect(screen.getByText('Development discussions')).toBeInTheDocument();
  });

  it('displays agent count for each room', () => {
    render(<RoomList />);

    expect(screen.getByText('2 agents')).toBeInTheDocument();
    expect(screen.getByText('1 agents')).toBeInTheDocument();
  });

  it('displays room model when present', () => {
    render(<RoomList />);

    expect(screen.getByText('Model: default')).toBeInTheDocument();
  });

  it('filters rooms based on search input', () => {
    render(<RoomList />);

    const searchInput = screen.getByPlaceholderText('Search rooms...');
    fireEvent.change(searchInput, { target: { value: 'dev' } });

    expect(screen.getByText('Dev Room')).toBeInTheDocument();
    expect(screen.queryByText('Lobby')).not.toBeInTheDocument();
  });

  it('filters rooms by description', () => {
    render(<RoomList />);

    const searchInput = screen.getByPlaceholderText('Search rooms...');
    fireEvent.change(searchInput, { target: { value: 'Main' } });

    expect(screen.getByText('Lobby')).toBeInTheDocument();
    expect(screen.queryByText('Dev Room')).not.toBeInTheDocument();
  });

  it('calls selectRoom when a room is clicked', () => {
    render(<RoomList />);

    const lobbyCard = screen.getByText('Lobby').closest('.cursor-pointer');
    fireEvent.click(lobbyCard!);

    expect(mockSelectRoom).toHaveBeenCalledWith('lobby');
  });

  it('highlights selected room', () => {
    (useConfigStore as any).mockReturnValue({
      rooms: mockRooms,
      selectedRoomId: 'lobby',
      selectRoom: mockSelectRoom,
      createRoom: mockCreateRoom,
    });

    render(<RoomList />);

    const lobbyCard = screen.getByText('Lobby').closest('.cursor-pointer');
    expect(lobbyCard).toHaveClass('ring-2', 'ring-orange-500');
  });

  it('shows create room form when Add button is clicked', () => {
    render(<RoomList />);

    const addButton = screen.getByText('Add');
    fireEvent.click(addButton);

    expect(screen.getByPlaceholderText('Room name...')).toBeInTheDocument();
  });

  it('creates new room with correct data', async () => {
    render(<RoomList />);

    const addButton = screen.getByText('Add');
    fireEvent.click(addButton);

    const input = screen.getByPlaceholderText('Room name...');
    fireEvent.change(input, { target: { value: 'Test Room' } });

    // Find the check button (submit button with check icon)
    const buttons = screen.getAllByRole('button');
    const checkButton = buttons.find(btn => btn.querySelector('.lucide-check'));
    fireEvent.click(checkButton!);

    await waitFor(() => {
      expect(mockCreateRoom).toHaveBeenCalledWith({
        display_name: 'Test Room',
        description: 'New room',
        agents: [],
      });
    });
  });

  it('cancels room creation when X button is clicked', () => {
    render(<RoomList />);

    const addButton = screen.getByText('Add');
    fireEvent.click(addButton);

    expect(screen.getByPlaceholderText('Room name...')).toBeInTheDocument();

    const cancelButton = screen.getAllByRole('button')[2]; // X button
    fireEvent.click(cancelButton);

    expect(screen.queryByPlaceholderText('Room name...')).not.toBeInTheDocument();
  });

  it('cancels room creation when Escape key is pressed', () => {
    render(<RoomList />);

    const addButton = screen.getByText('Add');
    fireEvent.click(addButton);

    const input = screen.getByPlaceholderText('Room name...');
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(screen.queryByPlaceholderText('Room name...')).not.toBeInTheDocument();
  });

  it('creates room when Enter key is pressed', async () => {
    render(<RoomList />);

    const addButton = screen.getByText('Add');
    fireEvent.click(addButton);

    const input = screen.getByPlaceholderText('Room name...');
    fireEvent.change(input, { target: { value: 'Enter Test Room' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => {
      expect(mockCreateRoom).toHaveBeenCalledWith({
        display_name: 'Enter Test Room',
        description: 'New room',
        agents: [],
      });
    });
  });

  it('shows empty state when no rooms exist', () => {
    (useConfigStore as any).mockReturnValue({
      rooms: [],
      selectedRoomId: null,
      selectRoom: mockSelectRoom,
      createRoom: mockCreateRoom,
    });

    render(<RoomList />);

    expect(screen.getByText('No rooms found')).toBeInTheDocument();
    expect(screen.getByText('Click "Add" to create one')).toBeInTheDocument();
  });
});
