import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ListItem, ListPanel } from './ListPanel';

interface TestListItem extends ListItem {
  description?: string;
}

const renderPanel = (onCreateItem: (name?: string) => void | boolean | Promise<void | boolean>) => {
  render(
    <ListPanel<TestListItem>
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
});
