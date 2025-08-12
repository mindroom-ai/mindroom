import { useConfigStore } from '@/store/configStore';
import { Bot, MapPin } from 'lucide-react';
import { ListPanel, ListItem } from '@/components/shared/ListPanel';
import { ItemCard, ItemCardBadge } from '@/components/shared/ItemCard';

// Extend Agent type to be compatible with ListItem
interface AgentListItem extends ListItem {
  role: string;
  tools: string[];
  rooms: string[];
  // Add index signature to be compatible with ListItem
  [key: string]: any;
}

export function AgentList() {
  const { agents, selectedAgentId, selectAgent, createAgent } = useConfigStore();

  const handleCreateAgent = (agentName?: string) => {
    const newAgent = {
      display_name: agentName || 'New Agent',
      role: 'A new agent that needs configuration',
      tools: [],
      instructions: [],
      rooms: ['lobby'],
      num_history_runs: 5,
    };
    createAgent(newAgent);
  };

  const renderAgent = (agent: AgentListItem, isSelected: boolean) => {
    const badges: ItemCardBadge[] = [
      {
        content: `${agent.tools.length} tools`,
        variant: 'secondary' as const,
        icon: Bot,
      },
      {
        content: `${agent.rooms.length} rooms`,
        variant: 'secondary' as const,
        icon: MapPin,
      },
    ];

    return (
      <ItemCard
        id={agent.id}
        title={agent.display_name}
        description={agent.role}
        isSelected={isSelected}
        onClick={selectAgent}
        badges={badges}
      />
    );
  };

  return (
    <ListPanel<AgentListItem>
      title="Agents"
      icon={Bot}
      items={agents as AgentListItem[]}
      selectedId={selectedAgentId || undefined}
      onItemSelect={selectAgent}
      onCreateItem={handleCreateAgent}
      renderItem={renderAgent}
      showSearch={true}
      searchPlaceholder="Search agents..."
      creationMode="inline-form"
      createButtonText="Add"
      createPlaceholder="Agent name..."
      emptyIcon={Bot}
      emptyMessage="No agents found"
      emptySubtitle={'Click "Add" to create one'}
    />
  );
}
