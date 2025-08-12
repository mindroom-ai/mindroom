import React, { useState } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Plus, Users, Settings2, X, Check } from 'lucide-react';
import { Badge } from '@/components/ui/badge';

export function RoomList() {
  const { rooms, selectedRoomId, selectRoom, createRoom } = useConfigStore();
  const [searchTerm, setSearchTerm] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [newRoomName, setNewRoomName] = useState('');

  const filteredRooms = rooms.filter(
    room =>
      room.display_name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      room.description?.toLowerCase().includes(searchTerm.toLowerCase())
  );

  const handleCreateRoom = () => {
    if (newRoomName.trim()) {
      createRoom({
        display_name: newRoomName.trim(),
        description: 'New room',
        agents: [],
      });
      setNewRoomName('');
      setIsCreating(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleCreateRoom();
    } else if (e.key === 'Escape') {
      setIsCreating(false);
      setNewRoomName('');
    }
  };

  return (
    <div className="h-full flex flex-col">
      <div className="p-4 border-b">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-xl font-semibold flex items-center gap-2">
            <Settings2 className="h-5 w-5" />
            Rooms
          </h2>
          <Button size="sm" variant="outline" onClick={() => setIsCreating(true)} className="gap-1">
            <Plus className="h-4 w-4" />
            New Room
          </Button>
        </div>
        <Input
          placeholder="Search rooms..."
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
          className="w-full"
        />
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-2">
        {isCreating && (
          <Card className="border-2 border-blue-500">
            <CardContent className="p-3">
              <div className="flex items-center gap-2">
                <Input
                  placeholder="Room name..."
                  value={newRoomName}
                  onChange={e => setNewRoomName(e.target.value)}
                  onKeyDown={handleKeyDown}
                  autoFocus
                  className="flex-1"
                />
                <Button size="sm" onClick={handleCreateRoom} variant="default">
                  <Check className="h-4 w-4" />
                </Button>
                <Button
                  size="sm"
                  onClick={() => {
                    setIsCreating(false);
                    setNewRoomName('');
                  }}
                  variant="ghost"
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
            </CardContent>
          </Card>
        )}

        {filteredRooms.length === 0 && !isCreating ? (
          <div className="text-center py-8 text-muted-foreground">
            <Settings2 className="h-12 w-12 mx-auto mb-3 opacity-20" />
            <p className="text-sm">No rooms found</p>
            <p className="text-xs mt-1">Click "New Room" to create one</p>
          </div>
        ) : (
          filteredRooms.map(room => (
            <Card
              key={room.id}
              className={`cursor-pointer transition-all hover:shadow-md hover:scale-[1.01] ${
                selectedRoomId === room.id
                  ? 'ring-2 ring-orange-500 bg-gradient-to-r from-orange-500/10 to-amber-500/10'
                  : ''
              }`}
              onClick={() => selectRoom(room.id)}
            >
              <CardContent className="p-4">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <h3 className="font-medium text-sm">{room.display_name}</h3>
                    {room.description && (
                      <p className="text-xs text-muted-foreground mt-1">{room.description}</p>
                    )}
                    <div className="flex items-center gap-2 mt-2">
                      <Badge variant="secondary" className="text-xs">
                        <Users className="h-3 w-3 mr-1" />
                        {room.agents.length} agents
                      </Badge>
                      {room.model && (
                        <Badge variant="outline" className="text-xs">
                          Model: {room.model}
                        </Badge>
                      )}
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          ))
        )}
      </div>
    </div>
  );
}
