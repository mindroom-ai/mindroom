import React, { ReactNode, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Plus, Search, Check, X, LucideIcon } from 'lucide-react';
import { cn } from '@/lib/utils';
import { sharedStyles } from './styles';
import { EmptyState } from './EmptyState';

export type CreationMode = 'instant' | 'inline-form' | 'dialog';

export interface ListItem {
  /**
   * Unique identifier for the item
   */
  id: string;
  /**
   * Display name/title of the item
   */
  display_name: string;
  /**
   * Additional data that can be used for filtering or display
   */
  [key: string]: unknown;
}

export interface ListPanelProps<T extends ListItem> {
  /**
   * Panel title
   */
  title: string;
  /**
   * Icon for the panel header
   */
  icon?: LucideIcon;
  /**
   * Array of items to display
   */
  items: T[];
  /**
   * ID of the currently selected item
   */
  selectedId?: string;
  /**
   * Function to handle item selection
   */
  onItemSelect?: (id: string) => void;
  /**
   * Function to handle item creation
   */
  onCreateItem?: (data?: string) => void;
  /**
   * Function to render each item
   */
  renderItem: (item: T, isSelected: boolean) => ReactNode;
  /**
   * Whether to show search functionality
   */
  showSearch?: boolean;
  /**
   * Custom search filter function
   */
  searchFilter?: (item: T, searchTerm: string) => boolean;
  /**
   * Placeholder text for search input
   */
  searchPlaceholder?: string;
  /**
   * Creation mode
   */
  creationMode?: CreationMode;
  /**
   * Text for the create button
   */
  createButtonText?: string;
  /**
   * Placeholder for creation input (for inline-form mode)
   */
  createPlaceholder?: string;
  /**
   * Icon for empty state
   */
  emptyIcon?: LucideIcon;
  /**
   * Empty state message
   */
  emptyMessage?: string;
  /**
   * Empty state subtitle
   */
  emptySubtitle?: string;
  /**
   * Additional CSS classes
   */
  className?: string;
  /**
   * Whether to show create button
   */
  showCreateButton?: boolean;
  /**
   * Creation form border color variant
   */
  creationBorderVariant?: 'blue' | 'orange';
}

/**
 * Default search filter function
 */
function defaultSearchFilter<T extends ListItem>(item: T, searchTerm: string): boolean {
  const term = searchTerm.toLowerCase();
  return (
    item.display_name.toLowerCase().includes(term) ||
    (typeof item.description === 'string' && item.description.toLowerCase().includes(term)) ||
    (typeof item.role === 'string' && item.role.toLowerCase().includes(term))
  );
}

/**
 * Reusable list sidebar component with consistent header, search, item rendering, and creation
 */
export function ListPanel<T extends ListItem>({
  title,
  icon: Icon,
  items,
  selectedId,
  onItemSelect: _onItemSelect,
  onCreateItem,
  renderItem,
  showSearch = false,
  searchFilter = defaultSearchFilter,
  searchPlaceholder,
  creationMode = 'instant',
  createButtonText = 'Add',
  createPlaceholder,
  emptyIcon: EmptyIcon,
  emptyMessage = 'No items found',
  emptySubtitle,
  className = '',
  showCreateButton = true,
  creationBorderVariant = 'blue',
}: ListPanelProps<T>) {
  const [searchTerm, setSearchTerm] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [newItemName, setNewItemName] = useState('');

  // Filter items based on search term
  const filteredItems = showSearch ? items.filter(item => searchFilter(item, searchTerm)) : items;

  const handleCreateItem = () => {
    if (creationMode === 'instant') {
      onCreateItem?.();
    } else if (creationMode === 'inline-form') {
      if (newItemName.trim()) {
        onCreateItem?.(newItemName.trim());
        setNewItemName('');
        setIsCreating(false);
      }
    } else if (creationMode === 'dialog') {
      onCreateItem?.();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleCreateItem();
    } else if (e.key === 'Escape') {
      setIsCreating(false);
      setNewItemName('');
    }
  };

  const containerClass = sharedStyles.panel.container;
  const headerClass = sharedStyles.panel.header;
  const contentClass = sharedStyles.panel.content;

  const creationBorderClass =
    creationBorderVariant === 'orange' ? 'border-2 border-orange-500' : 'border-2 border-blue-500';

  const headerContent = (
    <>
      <div className={sharedStyles.header.titleContainer}>
        <CardTitle className={Icon ? sharedStyles.header.titleWithIcon : undefined}>
          {Icon && <Icon className="h-5 w-5" />}
          {title}
        </CardTitle>
        {showCreateButton && onCreateItem && (
          <Button
            size="sm"
            variant="default"
            onClick={() => {
              if (creationMode === 'inline-form') {
                setIsCreating(true);
              } else {
                handleCreateItem();
              }
            }}
            className={sharedStyles.header.createButton}
            data-testid="create-button"
          >
            <Plus className="h-4 w-4 mr-1" />
            {createButtonText}
          </Button>
        )}
      </div>
      {showSearch && (
        <div className={`${sharedStyles.search.container} mt-3`}>
          <Search className={sharedStyles.search.icon} />
          <Input
            placeholder={searchPlaceholder || `Search ${title.toLowerCase()}...`}
            value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
            className={sharedStyles.search.input}
          />
        </div>
      )}
    </>
  );

  const contentArea = (
    <div className={contentClass}>
      {/* Inline creation form */}
      {isCreating && creationMode === 'inline-form' && (
        <Card className={creationBorderClass}>
          <CardContent className={sharedStyles.creation.content}>
            <div className={sharedStyles.creation.inputContainer}>
              <Input
                placeholder={createPlaceholder || `${title.slice(0, -1)} name...`}
                value={newItemName}
                onChange={e => setNewItemName(e.target.value)}
                onKeyDown={handleKeyDown}
                autoFocus
                className={sharedStyles.creation.input}
              />
              <Button
                size="sm"
                onClick={handleCreateItem}
                variant="default"
                data-testid="form-create-button"
              >
                <Check className="h-4 w-4" />
              </Button>
              <Button
                size="sm"
                onClick={() => {
                  setIsCreating(false);
                  setNewItemName('');
                }}
                variant="ghost"
                data-testid="form-cancel-button"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Empty state */}
      {filteredItems.length === 0 && !isCreating && EmptyIcon && (
        <EmptyState
          icon={EmptyIcon}
          title={emptyMessage}
          subtitle={emptySubtitle || `Click "${createButtonText}" to create one`}
        />
      )}

      {/* Items list */}
      {filteredItems.length > 0 && (
        <div className={sharedStyles.list.containerWithSpacing}>
          {filteredItems.map(item => (
            <div key={item.id}>{renderItem(item, selectedId === item.id)}</div>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <Card className={cn(containerClass, className)}>
      <CardHeader className={headerClass}>{headerContent}</CardHeader>
      {contentArea}
    </Card>
  );
}
