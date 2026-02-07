import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ListItem, ListPanel } from './ListPanel';

const renderPanel = (onCreateItem: (name?: string) => void | boolean | Promise<void | boolean>) => {
  render(
    <ListPanel<ListItem>
      title="Skills"
      items={[]}
      onCreateItem={onCreateItem}
      renderItem={() => null}
      creationMode="inline-form"
      createButtonText="New"
      createPlaceholder="Skill name..."
    />
  );
};

describe('ListPanel', () => {
  it('keeps inline creation form open when create returns false', async () => {
    const onCreateItem = vi.fn().mockResolvedValue(false);
    renderPanel(onCreateItem);

    fireEvent.click(screen.getByTestId('create-button'));
    fireEvent.change(screen.getByPlaceholderText('Skill name...'), {
      target: { value: 'New' },
    });
    fireEvent.click(screen.getByTestId('form-create-button'));

    await waitFor(() => {
      expect(onCreateItem).toHaveBeenCalledWith('New');
    });

    expect(screen.getByPlaceholderText('Skill name...')).toBeInTheDocument();
    expect((screen.getByPlaceholderText('Skill name...') as HTMLInputElement).value).toBe('New');
  });

  it('closes inline creation form when create succeeds', async () => {
    const onCreateItem = vi.fn().mockResolvedValue(true);
    renderPanel(onCreateItem);

    fireEvent.click(screen.getByTestId('create-button'));
    fireEvent.change(screen.getByPlaceholderText('Skill name...'), {
      target: { value: 'new' },
    });
    fireEvent.click(screen.getByTestId('form-create-button'));

    await waitFor(() => {
      expect(screen.queryByPlaceholderText('Skill name...')).not.toBeInTheDocument();
    });
  });

  it('closes inline creation form on Enter key when create succeeds', async () => {
    const onCreateItem = vi.fn().mockResolvedValue(true);
    renderPanel(onCreateItem);

    fireEvent.click(screen.getByTestId('create-button'));
    const input = screen.getByPlaceholderText('Skill name...');
    fireEvent.change(input, { target: { value: 'new' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => {
      expect(onCreateItem).toHaveBeenCalledWith('new');
    });

    await waitFor(() => {
      expect(screen.queryByPlaceholderText('Skill name...')).not.toBeInTheDocument();
    });
  });

  it('keeps inline creation form open on Enter key when create fails', async () => {
    const onCreateItem = vi.fn().mockResolvedValue(false);
    renderPanel(onCreateItem);

    fireEvent.click(screen.getByTestId('create-button'));
    const input = screen.getByPlaceholderText('Skill name...');
    fireEvent.change(input, { target: { value: 'bad!' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => {
      expect(onCreateItem).toHaveBeenCalledWith('bad!');
    });

    expect(screen.getByPlaceholderText('Skill name...')).toBeInTheDocument();
    expect((screen.getByPlaceholderText('Skill name...') as HTMLInputElement).value).toBe('bad!');
  });

  it('prevents duplicate create submissions while async create is in flight', async () => {
    let resolveCreate: ((value: boolean) => void) | undefined;
    const onCreateItem = vi.fn().mockImplementation(
      () =>
        new Promise<boolean>(resolve => {
          resolveCreate = resolve;
        })
    );
    renderPanel(onCreateItem);

    fireEvent.click(screen.getByTestId('create-button'));
    fireEvent.change(screen.getByPlaceholderText('Skill name...'), {
      target: { value: 'new' },
    });

    const submitButton = screen.getByTestId('form-create-button');
    fireEvent.click(submitButton);
    fireEvent.click(submitButton);

    expect(onCreateItem).toHaveBeenCalledTimes(1);
    expect(submitButton).toBeDisabled();
    expect(screen.getByPlaceholderText('Skill name...')).toBeDisabled();

    resolveCreate?.(true);

    await waitFor(() => {
      expect(screen.queryByPlaceholderText('Skill name...')).not.toBeInTheDocument();
    });
  });
});
