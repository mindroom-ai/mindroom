import { useConfigStore } from '@/store/configStore';
import { Home, Bot, Brain } from 'lucide-react';
import { ListPanel, ListItem } from '@/components/shared/ListPanel';
import { ItemCard, ItemCardBadge } from '@/components/shared/ItemCard';

// Extend Room type to be compatible with ListItem
interface RoomListItem extends ListItem {
  description?: string;
  agents: string[];
  model?: string;
  // Add index signature to be compatible with ListItem
  [key: string]: any;
}

export function RoomList() {
  const { rooms, selectedRoomId, selectRoom, createRoom } = useConfigStore();

  const handleCreateRoom = (roomName?: string) => {
    createRoom({
      display_name: roomName || 'New Room',
      description: 'New room',
      agents: [],
    });
  };

  const renderRoom = (room: RoomListItem, isSelected: boolean) => {
    const badges: ItemCardBadge[] = [
      {
        content: `${room.agents.length} agents`,
        variant: 'secondary' as const,
        icon: Bot,
      },
    ];

    if (room.model) {
      badges.push({
        content: `Model: ${room.model}`,
        variant: 'outline' as const,
        icon: Brain,
      });
    }

    return (
      <ItemCard
        id={room.id}
        title={room.display_name}
        description={room.description}
        isSelected={isSelected}
        onClick={selectRoom}
        badges={badges}
      />
    );
  };

  return (
    <ListPanel<RoomListItem>
      title="Rooms"
      icon={Home}
      items={rooms as RoomListItem[]}
      selectedId={selectedRoomId || undefined}
      onItemSelect={selectRoom}
      onCreateItem={handleCreateRoom}
      renderItem={renderRoom}
      showSearch={true}
      searchPlaceholder="Search rooms..."
      creationMode="inline-form"
      createButtonText="Add"
      createPlaceholder="Room name..."
      emptyIcon={Home}
      emptyMessage="No rooms found"
      emptySubtitle={'Click "Add" to create one'}
    />
  );
}
