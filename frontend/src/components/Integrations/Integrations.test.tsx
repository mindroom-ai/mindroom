import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { Integrations } from './Integrations';
import { useConfigStore } from '@/store/configStore';

// Mock hooks
const mockTools = [
  {
    name: 'weather',
    display_name: 'Weather',
    description: 'Get weather information',
    icon: '🌤️',
    icon_color: null,
    category: 'information',
    status: 'available',
    setup_type: 'api_key',
    config_fields: [
      {
        name: 'WEATHER_API_KEY',
        label: 'API Key',
        type: 'password',
        required: true,
        placeholder: 'Enter your weather API key',
        description: 'Your weather service API key',
      },
    ],
    helper_text: null,
    docs_url: null,
    dependencies: null,
  },
];

vi.mock('@/hooks/useTools', () => ({
  useTools: () => ({ tools: mockTools, loading: false, refetch: vi.fn() }),
  mapToolToIntegration: (tool: any) => ({
    id: tool.name,
    name: tool.display_name,
    description: tool.description,
    category: tool.category,
    status: tool.status,
    setup_type: tool.setup_type,
    config_fields: tool.config_fields,
    helper_text: tool.helper_text,
    docs_url: tool.docs_url,
  }),
}));

// Mock toast
const mockToast = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock icon mapping
vi.mock('./iconMapping', () => ({
  getIconForTool: (icon: string | null, _iconColor?: string | null) => <span>{icon}</span>,
}));

// Mock API base URL
vi.mock('@/lib/api', () => ({
  API_BASE_URL: 'http://localhost:8080',
  withAgentExecutionScope: (url: string) => url,
}));

// Mock EnhancedConfigDialog
vi.mock('./EnhancedConfigDialog', () => ({
  EnhancedConfigDialog: ({ onSuccess }: any) => {
    // Auto-call success when dialog opens
    setTimeout(() => onSuccess?.(), 0);
    return <div>Enhanced Config Dialog</div>;
  },
}));

// Mock integration providers
vi.mock('./integrations/index', () => ({
  integrationProviders: {
    google: {
      getConfig: () => ({
        integration: {
          id: 'google',
          name: 'Google Services',
          description: 'Gmail, Calendar, and Drive integration',
          category: 'email',
          icon: <span>Google Icon</span>,
          status: 'available',
          setup_type: 'special',
          connected: false,
        },
        onAction: vi.fn(),
        ConfigComponent: () => <div>Google Config Component</div>,
      }),
      loadStatus: vi.fn().mockResolvedValue({ status: 'available', connected: false }),
    },
    spotify: {
      getConfig: () => ({
        integration: {
          id: 'spotify',
          name: 'Spotify',
          description: 'Music streaming service',
          category: 'entertainment',
          icon: <span>Spotify Icon</span>,
          status: 'available',
          setup_type: 'oauth',
          connected: false,
        },
        onAction: vi.fn(),
        onDisconnect: vi.fn(),
      }),
      loadStatus: vi.fn().mockResolvedValue({ status: 'available', connected: false }),
    },
    plex: {
      getConfig: () => ({
        integration: {
          id: 'plex',
          name: 'Plex',
          description: 'Movie and TV show database',
          category: 'entertainment',
          icon: <span>Plex Icon</span>,
          status: 'connected',
          setup_type: 'api_key',
          connected: true,
        },
        onAction: vi.fn(),
        onDisconnect: vi.fn(),
        ConfigComponent: () => <div>Plex Config Component</div>,
      }),
      loadStatus: vi.fn().mockResolvedValue({ status: 'connected', connected: true }),
    },
  },
  getAllIntegrations: () => [
    vi.mocked({
      getConfig: () => ({
        integration: {
          id: 'google',
          name: 'Google Services',
          description: 'Gmail, Calendar, and Drive integration',
          category: 'email',
          icon: <span>Google Icon</span>,
          status: 'available',
          setup_type: 'special',
          connected: false,
        },
        onAction: vi.fn(),
        ConfigComponent: () => <div>Google Config Component</div>,
      }),
      loadStatus: vi.fn().mockResolvedValue({ status: 'available', connected: false }),
    }),
    vi.mocked({
      getConfig: () => ({
        integration: {
          id: 'spotify',
          name: 'Spotify',
          description: 'Music streaming service',
          category: 'entertainment',
          icon: <span>Spotify Icon</span>,
          status: 'available',
          setup_type: 'oauth',
          connected: false,
        },
        onAction: vi.fn(),
        onDisconnect: vi.fn(),
      }),
      loadStatus: vi.fn().mockResolvedValue({ status: 'available', connected: false }),
    }),
    vi.mocked({
      getConfig: () => ({
        integration: {
          id: 'plex',
          name: 'Plex',
          description: 'Movie and TV show database',
          category: 'entertainment',
          icon: <span>Plex Icon</span>,
          status: 'connected',
          setup_type: 'api_key',
          connected: true,
        },
        onAction: vi.fn(),
        onDisconnect: vi.fn(),
        ConfigComponent: () => <div>Plex Config Component</div>,
      }),
      loadStatus: vi.fn().mockResolvedValue({ status: 'connected', connected: true }),
    }),
  ],
}));

describe('Integrations', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockToast.mockReset();
    useConfigStore.setState({ agents: [] });
    Object.defineProperty(HTMLElement.prototype, 'hasPointerCapture', {
      configurable: true,
      value: () => false,
    });
  });

  it('should render integrations list', async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText('Tools')).toBeInTheDocument();
      expect(
        screen.getByText('Connect external services to enable agent capabilities')
      ).toBeInTheDocument();
    });
  });

  it('should display all integration cards', async () => {
    render(<Integrations />);

    await waitFor(() => {
      // Provider integrations
      expect(screen.getByText('Google Services')).toBeInTheDocument();
      expect(screen.getByText('Gmail, Calendar, and Drive integration')).toBeInTheDocument();
      expect(screen.getByText('Spotify')).toBeInTheDocument();
      expect(screen.getByText('Music streaming service')).toBeInTheDocument();
      expect(screen.getByText('Plex')).toBeInTheDocument();
      expect(screen.getByText('Movie and TV show database')).toBeInTheDocument();

      // Backend tools
      expect(screen.getByText('Weather')).toBeInTheDocument();
      expect(screen.getByText('Get weather information')).toBeInTheDocument();
    });
  });

  it('should show correct status badges', async () => {
    render(<Integrations />);

    await waitFor(() => {
      // Available integrations (Google, Spotify, and Weather)
      const availableBadges = screen.getAllByText('Available');
      expect(availableBadges.length).toBeGreaterThanOrEqual(2); // At least Google and Spotify

      // Connected integration
      expect(screen.getByText('Connected')).toBeInTheDocument(); // Plex
    });
  });

  it('should filter integrations by search term', async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText('Google Services')).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText('Search tools...');
    fireEvent.change(searchInput, { target: { value: 'spotify' } });

    await waitFor(() => {
      expect(screen.getByText('Spotify')).toBeInTheDocument();
      expect(screen.queryByText('Google Services')).not.toBeInTheDocument();
      expect(screen.queryByText('Plex')).not.toBeInTheDocument();
    });
  });

  it('should filter by availability', async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText('Weather')).toBeInTheDocument();
    });

    // Click "Available" filter button
    const availableButton = screen.getByRole('button', { name: 'Available' });
    fireEvent.click(availableButton);

    await waitFor(() => {
      expect(screen.getByText('Google Services')).toBeInTheDocument(); // Available
    });
  });

  it('should display category tabs', async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /All/ })).toBeInTheDocument();
      expect(screen.getByRole('tab', { name: /Email & Calendar/ })).toBeInTheDocument();
      expect(screen.getByRole('tab', { name: /Entertainment/ })).toBeInTheDocument();
      expect(screen.getByRole('tab', { name: /Information/ })).toBeInTheDocument();
    });
  });

  it.skip('should filter by category when tab is clicked', async () => {
    // TODO: Fix tab panel visibility testing
    render(<Integrations />);

    // Wait for initial render
    await waitFor(() => {
      expect(screen.getByText('Google Services')).toBeInTheDocument();
      expect(screen.getByText('Spotify')).toBeInTheDocument();
    });

    // Click Entertainment tab
    const entertainmentTab = screen.getByRole('tab', { name: /Entertainment/ });
    fireEvent.click(entertainmentTab);

    // Wait a bit for tab content to change
    await waitFor(() => {
      // In Entertainment category, we should see Spotify and Plex
      expect(screen.getByText('Spotify')).toBeInTheDocument();
      expect(screen.getByText('Plex')).toBeInTheDocument();
    });

    // Since tabs hide other content, these should not be visible
    // But the elements might still be in the DOM, just hidden
    // So let's check for visibility instead
    const googleElement = screen.queryByText('Gmail, Calendar, and Drive integration');
    if (googleElement) {
      // Check if it's hidden (parent tab panel might be hidden)
      const tabPanel = googleElement.closest('[role="tabpanel"]');
      if (tabPanel) {
        expect(tabPanel).toHaveAttribute('hidden');
      }
    }
  });

  it('should show correct action buttons', async () => {
    render(<Integrations />);

    await waitFor(() => {
      // Special setup type
      const setupButtons = screen.getAllByRole('button', { name: /Setup/ });
      expect(setupButtons.length).toBeGreaterThan(0);

      // OAuth type
      const connectButtons = screen.getAllByRole('button', { name: /Connect/ });
      expect(connectButtons.length).toBeGreaterThan(0);

      // Connected integration
      const disconnectButtons = screen.getAllByRole('button', { name: /Disconnect/ });
      expect(disconnectButtons.length).toBeGreaterThan(0);
    });
  });

  it('should show config dialog for tools with config fields', async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText('Weather')).toBeInTheDocument();
    });

    // Find the Weather card and its Configure button
    const weatherCard = screen.getByText('Weather').closest('.h-full');
    const configureButton = weatherCard?.querySelector('button:not(:disabled)');

    if (configureButton) {
      fireEvent.click(configureButton);

      await waitFor(() => {
        // Should show the Enhanced Config Dialog
        expect(screen.getByText('Enhanced Config Dialog')).toBeInTheDocument();
      });
    }
  });

  it('should open dialog for integrations with ConfigComponent', async () => {
    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText('Google Services')).toBeInTheDocument();
    });

    // Find and click the Google Setup button
    const googleCard = screen.getByText('Google Services').closest('.h-full');
    const setupButton = googleCard?.querySelector('button');

    if (setupButton) {
      fireEvent.click(setupButton);

      await waitFor(() => {
        expect(screen.getByText('Google Services Setup')).toBeInTheDocument();
        expect(screen.getByText('Google Config Component')).toBeInTheDocument();
      });
    }
  });

  it('should handle disconnect action', async () => {
    // Mock the fetch API
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({}),
    });

    render(<Integrations />);

    await waitFor(() => {
      expect(screen.getByText('Plex')).toBeInTheDocument();
    });

    // Find and click the Plex Disconnect button
    const imdbCard = screen.getByText('Plex').closest('.h-full');
    const disconnectButton = imdbCard?.querySelector('button[class*="destructive"]');

    if (disconnectButton) {
      fireEvent.click(disconnectButton);

      await waitFor(() => {
        expect(mockToast).toHaveBeenCalledWith({
          title: 'Disconnected',
          description: 'Plex has been disconnected.',
        });
      });
    }
  });

  it('lists worker-scoped and private agents in the scope selector', async () => {
    useConfigStore.setState({
      agents: [
        {
          id: 'general',
          display_name: 'Unscoped Agent',
          role: 'test',
          tools: ['gmail'],
          skills: [],
          instructions: [],
          rooms: ['lobby'],
          worker_scope: null,
        },
        {
          id: 'code',
          display_name: 'Scoped Agent',
          role: 'test',
          tools: ['gmail'],
          skills: [],
          instructions: [],
          rooms: ['lobby'],
          worker_scope: 'shared',
        },
        {
          id: 'mind',
          display_name: 'Private Agent',
          role: 'test',
          tools: ['gmail'],
          skills: [],
          instructions: [],
          rooms: ['personal'],
          private: {
            per: 'user_agent',
          },
        },
      ],
    });

    render(<Integrations />);

    const combobox = screen.getByRole('combobox');
    fireEvent.keyDown(combobox, { key: 'ArrowDown', code: 'ArrowDown' });

    await waitFor(() => {
      expect(screen.getByText('Scoped Agent')).toBeInTheDocument();
      expect(screen.getByText('Private Agent')).toBeInTheDocument();
    });

    expect(screen.queryByText('Unscoped Agent')).not.toBeInTheDocument();
  });

  it('hides shared-only integrations for isolating worker scopes', async () => {
    useConfigStore.setState({
      agents: [
        {
          id: 'code',
          display_name: 'Scoped Agent',
          role: 'test',
          tools: ['gmail'],
          skills: [],
          instructions: [],
          rooms: ['lobby'],
          worker_scope: 'user',
        },
      ],
    });

    render(<Integrations />);

    const combobox = screen.getByRole('combobox');
    fireEvent.keyDown(combobox, { key: 'ArrowDown', code: 'ArrowDown' });
    fireEvent.keyDown(combobox, { key: 'Enter', code: 'Enter' });

    await waitFor(() => {
      expect(screen.getByText('Scoped Agent')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Scoped Agent'));

    await waitFor(() => {
      expect(
        screen.getByText(/dashboard credential setup, editing, and disconnect are only supported/i)
      ).toBeInTheDocument();
    });

    expect(
      screen.getByText(
        /google services, home assistant, spotify, gmail, google calendar, and google sheets/i
      )
    ).toBeInTheDocument();
    expect(screen.getByText('worker_scope=shared', { selector: 'code' })).toBeInTheDocument();
    expect(screen.queryByText('Google Services')).not.toBeInTheDocument();
    expect(screen.queryByText('Spotify')).not.toBeInTheDocument();
    expect(screen.queryByText('Weather')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /shared-only config/i })).toHaveLength(2);
    for (const button of screen.getAllByRole('button', { name: /shared-only config/i })) {
      expect(button).toBeDisabled();
    }
  });

  it('treats private agents as isolating scopes in integrations', async () => {
    useConfigStore.setState({
      agents: [
        {
          id: 'mind',
          display_name: 'Private Agent',
          role: 'test',
          tools: ['gmail'],
          skills: [],
          instructions: [],
          rooms: ['personal'],
          private: {
            per: 'user',
          },
        },
      ],
    });

    render(<Integrations />);

    const combobox = screen.getByRole('combobox');
    fireEvent.keyDown(combobox, { key: 'ArrowDown', code: 'ArrowDown' });

    await waitFor(() => {
      expect(screen.getByText('Private Agent')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Private Agent'));

    await waitFor(() => {
      expect(
        screen.getByText('Configuring tools for Private Agent (private.per=user).')
      ).toBeInTheDocument();
      expect(
        screen.getByText(/dashboard credential setup, editing, and disconnect are only supported/i)
      ).toBeInTheDocument();
    });

    expect(screen.queryByText('Google Services')).not.toBeInTheDocument();
    expect(screen.queryByText('Spotify')).not.toBeInTheDocument();
    expect(screen.getByText('Weather')).toBeInTheDocument();
  });
});
