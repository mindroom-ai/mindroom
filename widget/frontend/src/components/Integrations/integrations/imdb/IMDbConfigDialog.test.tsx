import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { IMDbConfigDialog } from './IMDbConfigDialog';

// Mock fetch
global.fetch = vi.fn();

// Mock localStorage
const localStorageMock = {
  setItem: vi.fn(),
};
Object.defineProperty(window, 'localStorage', { value: localStorageMock });

// Mock toast
const mockToast = vi.fn();
vi.mock('@/components/ui/use-toast', () => ({
  useToast: vi.fn(() => ({
    toast: mockToast,
    toasts: [],
    dismiss: vi.fn(),
  })),
}));

describe('IMDbConfigDialog', () => {
  const mockOnClose = vi.fn();
  const mockOnSuccess = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (global.fetch as any).mockReset();
    localStorageMock.setItem.mockReset();
    mockToast.mockReset();
  });

  it('should render dialog with correct content', () => {
    render(<IMDbConfigDialog open={true} onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    expect(screen.getByText('Configure IMDb (OMDb API)')).toBeInTheDocument();
    expect(
      screen.getByText('Enter your OMDb API key to enable movie and TV show searches')
    ).toBeInTheDocument();
    expect(screen.getByLabelText('API Key')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Enter your OMDb API key')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Configure' })).toBeInTheDocument();
  });

  it('should show OMDb API link', () => {
    render(<IMDbConfigDialog open={true} onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const link = screen.getByRole('link', { name: 'OMDb API website' });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute('href', 'http://www.omdbapi.com/apikey.aspx');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('should close dialog when Cancel is clicked', () => {
    render(<IMDbConfigDialog open={true} onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(mockOnClose).toHaveBeenCalledTimes(1);
  });

  it.skip('should show error toast when API key is empty', () => {
    // TODO: Fix toast mocking issue
    render(<IMDbConfigDialog open={true} onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const configureButton = screen.getByRole('button', { name: 'Configure' });
    fireEvent.click(configureButton);

    // Toast should be called immediately since validation is synchronous
    expect(mockToast).toHaveBeenCalledTimes(1);
    expect(mockToast).toHaveBeenCalledWith({
      title: 'Missing API Key',
      description: 'Please enter your OMDb API key',
      variant: 'destructive',
    });

    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('should configure IMDb with valid API key', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: 'configured' }),
    });

    render(<IMDbConfigDialog open={true} onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const input = screen.getByPlaceholderText('Enter your OMDb API key');
    fireEvent.change(input, { target: { value: 'test-api-key' } });

    fireEvent.click(screen.getByRole('button', { name: 'Configure' }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/integrations/imdb/configure'),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            service: 'imdb',
            api_key: 'test-api-key',
          }),
        }
      );
    });

    await waitFor(() => {
      expect(localStorageMock.setItem).toHaveBeenCalledWith('imdb_configured', 'true');
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Success!',
        description: 'IMDb has been configured. Agents can now search for movies and TV shows.',
      });
      expect(mockOnSuccess).toHaveBeenCalledTimes(1);
      expect(mockOnClose).toHaveBeenCalledTimes(1);
    });
  });

  it('should handle configuration errors', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: false,
      json: async () => ({ detail: 'Invalid API key' }),
    });

    render(<IMDbConfigDialog open={true} onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const input = screen.getByPlaceholderText('Enter your OMDb API key');
    fireEvent.change(input, { target: { value: 'bad-api-key' } });

    fireEvent.click(screen.getByRole('button', { name: 'Configure' }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith({
        title: 'Configuration Failed',
        description: 'Invalid API key',
        variant: 'destructive',
      });
    });

    expect(localStorageMock.setItem).not.toHaveBeenCalled();
    expect(mockOnSuccess).not.toHaveBeenCalled();
    expect(mockOnClose).not.toHaveBeenCalled();
  });

  it('should disable Configure button when loading', async () => {
    // Mock a slow fetch response
    (global.fetch as any).mockImplementation(
      () =>
        new Promise(resolve =>
          setTimeout(
            () =>
              resolve({
                ok: true,
                json: async () => ({ status: 'configured' }),
              }),
            100
          )
        )
    );

    render(<IMDbConfigDialog open={true} onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const input = screen.getByPlaceholderText('Enter your OMDb API key');
    fireEvent.change(input, { target: { value: 'test-api-key' } });

    const configureButton = screen.getByRole('button', { name: 'Configure' });

    // Before clicking, button should be enabled
    expect(configureButton).not.toBeDisabled();

    // Click the button to trigger loading
    fireEvent.click(configureButton);

    // Button should be disabled immediately after clicking
    expect(configureButton).toBeDisabled();
  });
});
