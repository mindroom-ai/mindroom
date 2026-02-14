import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CultureList } from './CultureList';
import { useConfigStore } from '@/store/configStore';
import type { Culture } from '@/types/config';

vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

describe('CultureList', () => {
  const mockSelectCulture = vi.fn();
  const mockCreateCulture = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      cultures: [],
      selectedCultureId: null,
      selectCulture: mockSelectCulture,
      createCulture: mockCreateCulture,
    });
  });

  it('renders and filters cultures that do not have display_name', () => {
    const cultures: Culture[] = [
      {
        id: 'engineering',
        description: 'Shared engineering best practices',
        agents: ['code', 'shell'],
        mode: 'automatic',
      },
      {
        id: 'support',
        description: 'Support standards',
        agents: ['general'],
        mode: 'manual',
      },
    ];

    (useConfigStore as any).mockReturnValue({
      cultures,
      selectedCultureId: null,
      selectCulture: mockSelectCulture,
      createCulture: mockCreateCulture,
    });

    render(<CultureList />);

    expect(screen.getByText('engineering')).toBeInTheDocument();
    expect(screen.getByText('support')).toBeInTheDocument();

    const searchInput = screen.getByPlaceholderText('Search cultures...');
    fireEvent.change(searchInput, { target: { value: 'engineer' } });

    expect(screen.getByText('engineering')).toBeInTheDocument();
    expect(screen.queryByText('support')).not.toBeInTheDocument();
  });

  it('creates a culture with Enter and re-renders without crashing', async () => {
    let cultures: Culture[] = [];
    const createCulture = vi.fn((cultureData: Omit<Culture, 'id'>) => {
      cultures = [
        {
          id: 'engineering',
          ...cultureData,
        },
      ];
    });

    (useConfigStore as any).mockImplementation(() => ({
      cultures,
      selectedCultureId: null,
      selectCulture: mockSelectCulture,
      createCulture,
    }));

    const { rerender } = render(<CultureList />);

    fireEvent.click(screen.getByTestId('create-button'));
    const input = screen.getByPlaceholderText('Culture name...');
    fireEvent.change(input, { target: { value: 'Engineering' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => {
      expect(createCulture).toHaveBeenCalledWith({
        description: 'Engineering',
        agents: [],
        mode: 'automatic',
      });
    });

    rerender(<CultureList />);
    expect(screen.getByText('engineering')).toBeInTheDocument();
  });

  it('selects a culture when clicked', () => {
    const cultures: Culture[] = [
      {
        id: 'engineering',
        description: 'Shared engineering best practices',
        agents: ['code', 'shell'],
        mode: 'automatic',
      },
    ];

    (useConfigStore as any).mockReturnValue({
      cultures,
      selectedCultureId: null,
      selectCulture: mockSelectCulture,
      createCulture: mockCreateCulture,
    });

    render(<CultureList />);
    const cultureCard = screen.getByText('engineering').closest('.rounded-xl');
    fireEvent.click(cultureCard!);

    expect(mockSelectCulture).toHaveBeenCalledWith('engineering');
  });
});
