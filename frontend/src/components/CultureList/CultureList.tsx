import { useConfigStore } from '@/store/configStore';
import { BookOpen, Bot, Settings2 } from 'lucide-react';
import { ListPanel, ListItem } from '@/components/shared/ListPanel';
import { ItemCard, ItemCardBadge } from '@/components/shared/ItemCard';

interface CultureListItem extends ListItem {
  description: string;
  agents: string[];
  mode: string;
  [key: string]: any;
}

export function CultureList() {
  const { cultures, selectedCultureId, selectCulture, createCulture } = useConfigStore();

  const handleCreateCulture = (cultureName?: string) => {
    createCulture({
      description: cultureName || 'New culture',
      agents: [],
      mode: 'automatic',
    });
  };

  const renderCulture = (culture: CultureListItem, isSelected: boolean) => {
    const badges: ItemCardBadge[] = [
      {
        content: `${culture.agents.length} agents`,
        variant: 'secondary' as const,
        icon: Bot,
      },
      {
        content: `Mode: ${culture.mode}`,
        variant: 'outline' as const,
        icon: Settings2,
      },
    ];

    return (
      <ItemCard
        id={culture.id}
        title={culture.id}
        description={culture.description || 'No description'}
        isSelected={isSelected}
        onClick={selectCulture}
        badges={badges}
      />
    );
  };

  return (
    <ListPanel<CultureListItem>
      title="Cultures"
      icon={BookOpen}
      items={cultures as CultureListItem[]}
      selectedId={selectedCultureId || undefined}
      onItemSelect={selectCulture}
      onCreateItem={handleCreateCulture}
      renderItem={renderCulture}
      showSearch={true}
      searchPlaceholder="Search cultures..."
      creationMode="inline-form"
      createButtonText="Add"
      createPlaceholder="Culture name..."
      emptyIcon={BookOpen}
      emptyMessage="No cultures found"
      emptySubtitle={'Click "Add" to create one'}
      creationBorderVariant="orange"
    />
  );
}
