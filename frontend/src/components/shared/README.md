# Shared Components

This directory contains foundational UI components for standardizing the MindRoom widget interface. These components enforce consistent styling, behavior, and user experience across all list-based interfaces.

## Components

### ListPanel

A reusable list sidebar component that provides consistent header, search, item rendering, and creation functionality.

#### Features

- **Consistent header** with title, icon, and create button
- **Optional search functionality** with customizable filtering
- **Uniform item rendering** with selection states
- **Empty state handling** with custom icons and messages
- **Multiple creation modes**: instant, inline-form, dialog
- **Flexible variants**: card or panel layout

#### Usage

```tsx
import { ListPanel } from '@/components/shared';
import { Bot } from 'lucide-react';

const AgentListExample = () => {
  const agents = [
    { id: '1', display_name: 'Agent 1', description: 'First agent' },
    { id: '2', display_name: 'Agent 2', description: 'Second agent' },
  ];

  return (
    <ListPanel
      title="Agents"
      icon={Bot}
      items={agents}
      selectedId="1"
      onItemSelect={id => console.log('Selected:', id)}
      onCreateItem={() => console.log('Create agent')}
      renderItem={(agent, isSelected) => (
        <div className={isSelected ? 'bg-blue-100' : ''}>
          <h3>{agent.display_name}</h3>
          <p>{agent.description}</p>
        </div>
      )}
      showSearch={true}
      creationMode="inline-form"
      emptyIcon={Bot}
      emptyMessage="No agents found"
    />
  );
};
```

### ItemCard

A standardized card component for displaying list items with consistent selection states and badge support.

#### Usage

```tsx
import { ItemCard } from '@/components/shared';
import { Users } from 'lucide-react';

const TeamCardExample = () => (
  <ItemCard
    id="team-1"
    title="Development Team"
    description="Frontend and backend developers"
    isSelected={true}
    onClick={id => console.log('Selected team:', id)}
    badges={[
      { content: '5 agents', icon: Users, variant: 'secondary' },
      { content: 'Mode: coordinate', variant: 'outline' },
    ]}
  />
);
```

### EmptyState

A consistent empty state component for when lists or sections have no content.

#### Usage

```tsx
import { EmptyState } from '@/components/shared';
import { Settings2 } from 'lucide-react';

const EmptyRoomsExample = () => (
  <EmptyState icon={Settings2} title="No rooms found" subtitle='Click "New Room" to create one' />
);
```

### Shared Styles

Utility functions and consistent className patterns for common UI elements.

#### Usage

```tsx
import { sharedStyles, getSelectionStyles, getIconStyles } from '@/components/shared';

// Use predefined styles
<div className={sharedStyles.panel.container}>
  <header className={sharedStyles.panel.header}>
    <h1 className={sharedStyles.header.title}>Title</h1>
  </header>
</div>

// Use selection utilities
<button className={getSelectionStyles(isSelected, 'card')}>
  <Icon className={getIconStyles(isSelected)} />
</button>
```

## Design Principles

1. **Consistency**: All components follow the same visual patterns and behaviors
2. **Flexibility**: Components are configurable but enforce consistency
3. **Accessibility**: Built-in keyboard navigation and ARIA compliance
4. **Type Safety**: Full TypeScript support with comprehensive interfaces
5. **Reusability**: Generic interfaces support various data types

## Migration Guide

When refactoring existing components to use these shared components:

1. **Identify common patterns** in your current list components
2. **Replace custom layouts** with ListPanel
3. **Standardize item cards** using ItemCard
4. **Use consistent empty states** with EmptyState
5. **Apply shared styles** for uniform appearance

This approach reduces code duplication while ensuring a consistent user experience across the MindRoom widget interface.
