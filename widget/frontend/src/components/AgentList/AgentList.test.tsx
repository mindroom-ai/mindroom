import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { AgentList } from './AgentList';
import { useConfigStore } from '@/store/configStore';

// Mock the store
vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

describe('AgentList', () => {
  const mockAgents = [
    {
      id: 'agent1',
      display_name: 'Test Agent 1',
      role: 'Test role 1',
      tools: ['calculator', 'file'],
      instructions: ['instruction1'],
      rooms: ['lobby', 'dev'],
      num_history_runs: 5,
    },
    {
      id: 'agent2',
      display_name: 'Test Agent 2',
      role: 'Test role 2',
      tools: [],
      instructions: [],
      rooms: ['lobby'],
      num_history_runs: 3,
    },
  ];

  const mockSelectAgent = vi.fn();
  const mockCreateAgent = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      agents: mockAgents,
      selectedAgentId: null,
      selectAgent: mockSelectAgent,
      createAgent: mockCreateAgent,
    });
  });

  it('should render all agents', () => {
    render(<AgentList />);

    expect(screen.getByText('Test Agent 1')).toBeInTheDocument();
    expect(screen.getByText('Test Agent 2')).toBeInTheDocument();
    expect(screen.getByText('2 tools')).toBeInTheDocument();
    expect(screen.getByText('2 rooms')).toBeInTheDocument();
    expect(screen.getByText('0 tools')).toBeInTheDocument();
    expect(screen.getByText('1 rooms')).toBeInTheDocument();
  });

  it('should highlight selected agent', () => {
    (useConfigStore as any).mockReturnValue({
      agents: mockAgents,
      selectedAgentId: 'agent1',
      selectAgent: mockSelectAgent,
      createAgent: mockCreateAgent,
    });

    render(<AgentList />);

    const selectedAgent = screen.getByText('Test Agent 1').closest('div[role="button"]');
    expect(selectedAgent).toHaveClass('ring-2', 'ring-orange-500');
  });

  it('should call selectAgent when clicking an agent', () => {
    render(<AgentList />);

    const agent2Button = screen.getByText('Test Agent 2').closest('div[role="button"]');
    fireEvent.click(agent2Button!);

    expect(mockSelectAgent).toHaveBeenCalledWith('agent2');
  });

  it('should show creation form when clicking add button', () => {
    render(<AgentList />);

    const addButton = screen.getByText('Add');
    fireEvent.click(addButton);

    // Should show the inline form with input field
    const input = screen.getByPlaceholderText('Agent name...');
    expect(input).toBeInTheDocument();

    // Type a name and submit
    fireEvent.change(input, { target: { value: 'Test Agent' } });

    // Find and click the create button in the form
    const createButton = screen.getByTestId('form-create-button');
    fireEvent.click(createButton);

    expect(mockCreateAgent).toHaveBeenCalled();
  });

  it('should render add button when no agents', () => {
    (useConfigStore as any).mockReturnValue({
      agents: [],
      selectedAgentId: null,
      selectAgent: mockSelectAgent,
      createAgent: mockCreateAgent,
    });

    render(<AgentList />);

    // Should still show the Add button even with no agents
    expect(screen.getByText('Add')).toBeInTheDocument();
  });
});
