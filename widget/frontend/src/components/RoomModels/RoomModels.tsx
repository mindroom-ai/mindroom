import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Plus, X, Home } from 'lucide-react';
import { useState } from 'react';

export function RoomModels() {
  const { config, updateRoomModels } = useConfigStore();
  const [newRoom, setNewRoom] = useState('');
  const [isAdding, setIsAdding] = useState(false);

  const roomModels = config?.room_models || {};
  const models = config?.models || {};

  const handleAddRoom = () => {
    if (newRoom.trim() && !roomModels[newRoom]) {
      const updated = { ...roomModels, [newRoom]: 'default' };
      updateRoomModels(updated);
      setNewRoom('');
      setIsAdding(false);
    }
  };

  const handleUpdateModel = (room: string, model: string) => {
    const updated = { ...roomModels, [room]: model };
    updateRoomModels(updated);
  };

  const handleRemoveRoom = (room: string) => {
    const { [room]: _, ...rest } = roomModels;
    updateRoomModels(rest);
  };

  return (
    <Card className="h-full flex flex-col">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Home className="h-5 w-5" />
            Room-Specific Models
          </CardTitle>
          <Button size="sm" variant="outline" onClick={() => setIsAdding(true)}>
            <Plus className="h-4 w-4 mr-1" />
            Add Room
          </Button>
        </div>
        <p className="text-sm text-gray-600 mt-2">
          Configure which models teams should use in specific rooms. These override the team's
          default model.
        </p>
      </CardHeader>
      <CardContent className="flex-1 overflow-y-auto">
        <div className="space-y-4">
          {isAdding && (
            <div className="p-4 border rounded-lg bg-blue-50">
              <Label htmlFor="new-room">Room Name</Label>
              <div className="flex gap-2 mt-2">
                <Input
                  id="new-room"
                  placeholder="Enter room name..."
                  value={newRoom}
                  onChange={e => setNewRoom(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') handleAddRoom();
                    if (e.key === 'Escape') {
                      setIsAdding(false);
                      setNewRoom('');
                    }
                  }}
                  autoFocus
                />
                <Button size="sm" onClick={handleAddRoom}>
                  Add
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setIsAdding(false);
                    setNewRoom('');
                  }}
                >
                  Cancel
                </Button>
              </div>
            </div>
          )}

          {Object.entries(roomModels).length === 0 && !isAdding && (
            <div className="text-center py-8 text-gray-500">
              <Home className="h-12 w-12 mx-auto mb-2 text-gray-300" />
              <p>No room-specific models configured</p>
              <p className="text-sm mt-1">Click "Add Room" to configure room-specific models</p>
            </div>
          )}

          {Object.entries(roomModels).map(([room, model]) => (
            <div key={room} className="flex items-center gap-3 p-3 border rounded-lg">
              <div className="flex-1">
                <Label className="text-sm font-medium">{room}</Label>
              </div>
              <Select value={model} onValueChange={value => handleUpdateModel(room, value)}>
                <SelectTrigger className="w-48">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.keys(models).map(modelId => (
                    <SelectItem key={modelId} value={modelId}>
                      {modelId}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button size="icon" variant="ghost" onClick={() => handleRemoveRoom(room)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
