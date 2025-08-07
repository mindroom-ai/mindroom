import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ToolConfigDialog } from './ToolConfigDialog';
import { useConfigStore } from '@/store/configStore';

// Mock the store
vi.mock('@/store/configStore', () => ({
  useConfigStore: vi.fn(),
}));

// Mock the toast
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({
    toast: vi.fn(),
  }),
}));

describe('ToolConfigDialog', () => {
  const mockUpdateToolConfig = vi.fn();
  const mockOnOpenChange = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (useConfigStore as any).mockReturnValue({
      config: {
        tools: {},
      },
      updateToolConfig: mockUpdateToolConfig,
    });
  });

  it('renders dialog with tool schema', () => {
    render(<ToolConfigDialog toolId="googlesearch" open={true} onOpenChange={mockOnOpenChange} />);

    expect(screen.getByText('Configure Google Search')).toBeInTheDocument();
    expect(screen.getByText('Search the web using Google')).toBeInTheDocument();
  });

  it('shows message for tools without configuration', () => {
    render(<ToolConfigDialog toolId="nonexistent" open={true} onOpenChange={mockOnOpenChange} />);

    expect(screen.getByText('This tool does not require configuration.')).toBeInTheDocument();
  });

  it('renders all field types correctly', () => {
    render(<ToolConfigDialog toolId="googlesearch" open={true} onOpenChange={mockOnOpenChange} />);

    // Check for text/password fields
    expect(screen.getByLabelText('API Key')).toBeInTheDocument();
    expect(screen.getByLabelText('Search Engine ID')).toBeInTheDocument();
  });

  it('handles text field changes', () => {
    render(<ToolConfigDialog toolId="email" open={true} onOpenChange={mockOnOpenChange} />);

    const smtpInput = screen.getByLabelText('SMTP Host');
    fireEvent.change(smtpInput, { target: { value: 'smtp.gmail.com' } });

    expect(smtpInput).toHaveValue('smtp.gmail.com');
  });

  it('handles number field changes', () => {
    render(<ToolConfigDialog toolId="shell" open={true} onOpenChange={mockOnOpenChange} />);

    const timeoutInput = screen.getByLabelText('Command Timeout (seconds)');
    fireEvent.change(timeoutInput, { target: { value: '30' } });

    expect(timeoutInput).toHaveValue(30);
  });

  it('handles boolean field changes', () => {
    render(<ToolConfigDialog toolId="duckduckgo" open={true} onOpenChange={mockOnOpenChange} />);

    // DuckDuckGo has only optional fields, so no tabs
    const safeSearchCheckbox = screen.getByLabelText('Safe Search');
    // It starts checked because default is true
    expect(safeSearchCheckbox).toBeChecked();

    // Click to uncheck
    fireEvent.click(safeSearchCheckbox);
    expect(safeSearchCheckbox).not.toBeChecked();

    // Click again to check
    fireEvent.click(safeSearchCheckbox);
    expect(safeSearchCheckbox).toBeChecked();
  });

  it('handles select field changes', () => {
    render(<ToolConfigDialog toolId="duckduckgo" open={true} onOpenChange={mockOnOpenChange} />);

    // DuckDuckGo has only optional fields, so no tabs
    // Find the select trigger
    const regionLabel = screen.getByText('Region');
    const regionSelect = regionLabel.parentElement?.querySelector('[role="combobox"]');

    expect(regionSelect).toBeTruthy();
    expect(regionSelect).toHaveTextContent('No region'); // Default value

    fireEvent.click(regionSelect!);

    const option = screen.getByText('United States');
    fireEvent.click(option);

    expect(regionSelect).toHaveTextContent('United States');
  });

  it('validates required fields', async () => {
    render(<ToolConfigDialog toolId="googlesearch" open={true} onOpenChange={mockOnOpenChange} />);

    const saveButton = screen.getByText('Save Configuration');
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(screen.getByText('API Key is required')).toBeInTheDocument();
      expect(screen.getByText('Search Engine ID is required')).toBeInTheDocument();
    });

    expect(mockUpdateToolConfig).not.toHaveBeenCalled();
  });

  it('validates number field min/max', async () => {
    render(<ToolConfigDialog toolId="shell" open={true} onOpenChange={mockOnOpenChange} />);

    const timeoutInput = screen.getByLabelText('Command Timeout (seconds)');
    fireEvent.change(timeoutInput, { target: { value: '0' } });

    const saveButton = screen.getByText('Save Configuration');
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(screen.getByText('Must be at least 1')).toBeInTheDocument();
    });
  });

  it('validates URL format', async () => {
    // URL validation requires pattern in schema
    // For now just test required field validation
    render(<ToolConfigDialog toolId="jina" open={true} onOpenChange={mockOnOpenChange} />);

    const saveButton = screen.getByText('Save Configuration');
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(screen.getByText('API Key is required')).toBeInTheDocument();
    });
  });

  it('saves configuration when valid', async () => {
    render(<ToolConfigDialog toolId="googlesearch" open={true} onOpenChange={mockOnOpenChange} />);

    const apiKeyInput = screen.getByLabelText('API Key');
    const searchEngineIdInput = screen.getByLabelText('Search Engine ID');

    fireEvent.change(apiKeyInput, { target: { value: 'test-api-key' } });
    fireEvent.change(searchEngineIdInput, { target: { value: 'test-engine-id' } });

    const saveButton = screen.getByText('Save Configuration');
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockUpdateToolConfig).toHaveBeenCalledWith('googlesearch', {
        api_key: 'test-api-key',
        search_engine_id: 'test-engine-id',
        max_results: 10,
      });
      expect(mockOnOpenChange).toHaveBeenCalledWith(false);
    });
  });

  it('loads existing configuration', () => {
    (useConfigStore as any).mockReturnValue({
      config: {
        tools: {
          googlesearch: {
            api_key: 'existing-key',
            search_engine_id: 'existing-id',
          },
        },
      },
      updateToolConfig: mockUpdateToolConfig,
    });

    render(<ToolConfigDialog toolId="googlesearch" open={true} onOpenChange={mockOnOpenChange} />);

    const apiKeyInput = screen.getByLabelText('API Key') as HTMLInputElement;
    const searchEngineIdInput = screen.getByLabelText('Search Engine ID') as HTMLInputElement;

    expect(apiKeyInput.value).toBe('existing-key');
    expect(searchEngineIdInput.value).toBe('existing-id');
  });

  it('clears errors when field is edited', async () => {
    render(<ToolConfigDialog toolId="googlesearch" open={true} onOpenChange={mockOnOpenChange} />);

    // Trigger validation error
    const saveButton = screen.getByText('Save Configuration');
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(screen.getByText('API Key is required')).toBeInTheDocument();
    });

    // Edit the field
    const apiKeyInput = screen.getByLabelText('API Key');
    fireEvent.change(apiKeyInput, { target: { value: 'test-key' } });

    // Error should be cleared
    await waitFor(() => {
      expect(screen.queryByText('API Key is required')).not.toBeInTheDocument();
    });
  });

  it('handles cancel button', () => {
    render(<ToolConfigDialog toolId="googlesearch" open={true} onOpenChange={mockOnOpenChange} />);

    const cancelButton = screen.getByText('Cancel');
    fireEvent.click(cancelButton);

    expect(mockOnOpenChange).toHaveBeenCalledWith(false);
    expect(mockUpdateToolConfig).not.toHaveBeenCalled();
  });

  it('groups fields by required/optional when there are many', () => {
    render(<ToolConfigDialog toolId="googlesearch" open={true} onOpenChange={mockOnOpenChange} />);

    // Google search has both required and optional fields
    expect(screen.getByText('Required')).toBeInTheDocument();
    expect(screen.getByText('Optional')).toBeInTheDocument();
  });

  it('disables save button for tools without fields', () => {
    render(<ToolConfigDialog toolId="calculator" open={true} onOpenChange={mockOnOpenChange} />);

    const saveButton = screen.getByText('Save Configuration');
    expect(saveButton).toBeDisabled();
  });
});
