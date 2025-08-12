import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { TeamList } from './TeamList';
import { useConfigStore } from '@/store/configStore';
import { Team } from '@/types/config';

// Mock the store
vi.mock('@/store/configStore');

describe('TeamList', () => {
  const mockTeams: Team[] = [
    {
      id: 'dev_team',
      display_name: 'Dev Team',
      role: 'Development team for coding tasks',
      agents: ['code', 'shell'],
      rooms: ['dev', 'lobby'],
      mode: 'coordinate',
    },
    {
      id: 'analysis_team',
      display_name: 'Analysis Team',
      role: 'Data analysis and research team',
      agents: ['data_analyst', 'research'],
      rooms: ['analysis', 'lobby'],
      mode: 'collaborate',
    },
  ];

  const mockSelectTeam = vi.fn();
  const mockCreateTeam = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      teams: mockTeams,
      selectedTeamId: null,
      selectTeam: mockSelectTeam,
      createTeam: mockCreateTeam,
    });
  });

  it('renders team list correctly', () => {
    render(<TeamList />);

    expect(screen.getByText('Teams')).toBeInTheDocument();
    expect(screen.getByText('Dev Team')).toBeInTheDocument();
    expect(screen.getByText('Analysis Team')).toBeInTheDocument();
    expect(screen.getByText('Development team for coding tasks')).toBeInTheDocument();
    expect(screen.getByText('Data analysis and research team')).toBeInTheDocument();
  });

  it('displays agent count and mode for each team', () => {
    render(<TeamList />);

    // Both teams have 2 agents, so there should be 2 elements with "2 agents"
    const agentCounts = screen.getAllByText('2 agents');
    expect(agentCounts).toHaveLength(2);
    expect(screen.getByText('Mode: coordinate')).toBeInTheDocument();
    expect(screen.getByText('Mode: collaborate')).toBeInTheDocument();
  });

  it('filters teams based on search input', () => {
    render(<TeamList />);

    const searchInput = screen.getByPlaceholderText('Search teams...');
    fireEvent.change(searchInput, { target: { value: 'dev' } });

    expect(screen.getByText('Dev Team')).toBeInTheDocument();
    expect(screen.queryByText('Analysis Team')).not.toBeInTheDocument();
  });

  it('filters teams by role description', () => {
    render(<TeamList />);

    const searchInput = screen.getByPlaceholderText('Search teams...');
    fireEvent.change(searchInput, { target: { value: 'research' } });

    expect(screen.queryByText('Dev Team')).not.toBeInTheDocument();
    expect(screen.getByText('Analysis Team')).toBeInTheDocument();
  });

  it('calls selectTeam when a team is clicked', () => {
    render(<TeamList />);

    const devTeamCard = screen.getByText('Dev Team').closest('.rounded-xl');
    fireEvent.click(devTeamCard!);

    expect(mockSelectTeam).toHaveBeenCalledWith('dev_team');
  });

  it('highlights selected team', () => {
    (useConfigStore as any).mockReturnValue({
      teams: mockTeams,
      selectedTeamId: 'dev_team',
      selectTeam: mockSelectTeam,
      createTeam: mockCreateTeam,
    });

    render(<TeamList />);

    const devTeamCard = screen.getByText('Dev Team').closest('.rounded-xl');
    expect(devTeamCard).toHaveClass('ring-2', 'ring-orange-500');
  });

  it('shows create team form when plus button is clicked', () => {
    render(<TeamList />);

    // Find the create button
    const createButton = screen.getByTestId('create-button');
    fireEvent.click(createButton);

    expect(screen.getByPlaceholderText('Team name...')).toBeInTheDocument();
    expect(screen.getByTestId('form-create-button')).toBeInTheDocument();
    expect(screen.getByTestId('form-cancel-button')).toBeInTheDocument();
  });

  it('creates new team with correct data', async () => {
    render(<TeamList />);

    const createButton = screen.getByTestId('create-button');
    fireEvent.click(createButton);

    const input = screen.getByPlaceholderText('Team name...');
    fireEvent.change(input, { target: { value: 'Test Team' } });

    const formCreateButton = screen.getByTestId('form-create-button');
    fireEvent.click(formCreateButton);

    await waitFor(() => {
      expect(mockCreateTeam).toHaveBeenCalledWith({
        display_name: 'Test Team',
        role: 'New team description',
        agents: [],
        rooms: [],
        mode: 'coordinate',
      });
    });
  });

  it('cancels team creation when cancel button is clicked', () => {
    render(<TeamList />);

    const createButton = screen.getByTestId('create-button');
    fireEvent.click(createButton);

    expect(screen.getByPlaceholderText('Team name...')).toBeInTheDocument();

    const cancelButton = screen.getByTestId('form-cancel-button');
    fireEvent.click(cancelButton);

    expect(screen.queryByPlaceholderText('Team name...')).not.toBeInTheDocument();
  });

  it('cancels team creation when Escape key is pressed', () => {
    render(<TeamList />);

    const createButton = screen.getByTestId('create-button');
    fireEvent.click(createButton);

    const input = screen.getByPlaceholderText('Team name...');
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(screen.queryByPlaceholderText('Team name...')).not.toBeInTheDocument();
  });

  it('creates team when Enter key is pressed', async () => {
    render(<TeamList />);

    const createButton = screen.getByTestId('create-button');
    fireEvent.click(createButton);

    const input = screen.getByPlaceholderText('Team name...');
    fireEvent.change(input, { target: { value: 'Enter Test Team' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => {
      expect(mockCreateTeam).toHaveBeenCalledWith({
        display_name: 'Enter Test Team',
        role: 'New team description',
        agents: [],
        rooms: [],
        mode: 'coordinate',
      });
    });
  });
});
