import { useConfigStore } from '@/store/configStore';
import { Users, Bot, Settings } from 'lucide-react';
import { ListPanel, ListItem } from '@/components/shared/ListPanel';
import { ItemCard, ItemCardBadge } from '@/components/shared/ItemCard';

// Extend Team type to be compatible with ListItem
interface TeamListItem extends ListItem {
  role: string;
  agents: string[];
  mode: string;
  // Add index signature to be compatible with ListItem
  [key: string]: any;
}

export function TeamList() {
  const { teams, selectedTeamId, selectTeam, createTeam } = useConfigStore();

  const handleCreateTeam = (teamName?: string) => {
    createTeam({
      display_name: teamName || 'New Team',
      role: 'New team description',
      agents: [],
      rooms: [],
      mode: 'coordinate',
    });
  };

  const renderTeam = (team: TeamListItem, isSelected: boolean) => {
    const badges: ItemCardBadge[] = [
      {
        content: `${team.agents.length} agents`,
        variant: 'secondary' as const,
        icon: Bot,
      },
      {
        content: `Mode: ${team.mode}`,
        variant: 'outline' as const,
        icon: Settings,
      },
    ];

    return (
      <ItemCard
        id={team.id}
        title={team.display_name}
        description={team.role}
        isSelected={isSelected}
        onClick={selectTeam}
        badges={badges}
      />
    );
  };

  return (
    <ListPanel<TeamListItem>
      title="Teams"
      icon={Users}
      items={teams as TeamListItem[]}
      selectedId={selectedTeamId || undefined}
      onItemSelect={selectTeam}
      onCreateItem={handleCreateTeam}
      renderItem={renderTeam}
      showSearch={true}
      searchPlaceholder="Search teams..."
      creationMode="inline-form"
      createButtonText="Add"
      createPlaceholder="Team name..."
      emptyIcon={Users}
      emptyMessage="No teams found"
      emptySubtitle={'Click "Add" to create one'}
      creationBorderVariant="orange"
    />
  );
}
