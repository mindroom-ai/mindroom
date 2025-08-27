import { useState, useEffect } from 'react';
import { Trash2, RefreshCw, ExternalLink, AlertCircle } from 'lucide-react';
import { API_ENDPOINTS, fetchAPI } from '../../lib/api';
import { Button } from '../ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../ui/card';
import { Badge } from '../ui/badge';
import { Checkbox } from '../ui/checkbox';
import { Alert, AlertDescription } from '../ui/alert';
import { ScrollArea } from '../ui/scroll-area';
import { cn } from '../../lib/utils';

interface RoomInfo {
  room_id: string;
  name?: string;
}

interface AgentRoomsInfo {
  agent_id: string;
  display_name: string;
  configured_rooms: string[];
  joined_rooms: string[];
  unconfigured_rooms: string[];
  unconfigured_room_details?: RoomInfo[];
}

interface RoomLeaveRequest {
  agent_id: string;
  room_id: string;
}

export function UnconfiguredRooms() {
  const [agentsRooms, setAgentsRooms] = useState<AgentRoomsInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedRooms, setSelectedRooms] = useState<Set<string>>(new Set());
  const [leavingRooms, setLeavingRooms] = useState(false);

  // Load agent rooms data
  const loadAgentRooms = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchAPI(API_ENDPOINTS.matrix.agentsRooms);
      setAgentsRooms(response.agents);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load agent rooms');
      console.error('Error loading agent rooms:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAgentRooms();
  }, []);

  // Toggle room selection
  const toggleRoomSelection = (agentId: string, roomId: string) => {
    const key = `${agentId}:${roomId}`;
    setSelectedRooms(prev => {
      const newSet = new Set(prev);
      if (newSet.has(key)) {
        newSet.delete(key);
      } else {
        newSet.add(key);
      }
      return newSet;
    });
  };

  // Select all unconfigured rooms for an agent
  const selectAllForAgent = (agentId: string, rooms: string[]) => {
    setSelectedRooms(prev => {
      const newSet = new Set(prev);
      rooms.forEach(roomId => {
        newSet.add(`${agentId}:${roomId}`);
      });
      return newSet;
    });
  };

  // Deselect all unconfigured rooms for an agent
  const deselectAllForAgent = (agentId: string, rooms: string[]) => {
    setSelectedRooms(prev => {
      const newSet = new Set(prev);
      rooms.forEach(roomId => {
        newSet.delete(`${agentId}:${roomId}`);
      });
      return newSet;
    });
  };

  // Leave selected rooms
  const leaveSelectedRooms = async () => {
    if (selectedRooms.size === 0) return;

    setLeavingRooms(true);
    setError(null);

    try {
      const requests: RoomLeaveRequest[] = Array.from(selectedRooms).map(key => {
        // Split only on the first colon to preserve the room ID format (!room:server)
        const colonIndex = key.indexOf(':');
        const agent_id = key.substring(0, colonIndex);
        const room_id = key.substring(colonIndex + 1);
        return { agent_id, room_id };
      });

      const response = await fetchAPI(API_ENDPOINTS.matrix.leaveRoomsBulk, {
        method: 'POST',
        body: JSON.stringify(requests),
      });

      if (response.success) {
        // Clear selection and reload data
        setSelectedRooms(new Set());
        await loadAgentRooms();
      } else {
        // Handle partial failures
        const failed = response.results.filter((r: any) => !r.success);
        if (failed.length > 0) {
          setError(`Failed to leave ${failed.length} room(s). Please try again.`);
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to leave rooms');
      console.error('Error leaving rooms:', err);
    } finally {
      setLeavingRooms(false);
    }
  };

  // Calculate total unconfigured rooms
  const totalUnconfiguredRooms = agentsRooms.reduce(
    (sum, agent) => sum + agent.unconfigured_rooms.length,
    0
  );

  // Check if all rooms for an agent are selected
  const areAllSelectedForAgent = (agentId: string, rooms: string[]) => {
    return rooms.every(roomId => selectedRooms.has(`${agentId}:${roomId}`));
  };

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="h-20 w-full animate-pulse rounded-md bg-muted" />
        <div className="h-40 w-full animate-pulse rounded-md bg-muted" />
        <div className="h-40 w-full animate-pulse rounded-md bg-muted" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">External Rooms</h2>
          <p className="text-sm text-muted-foreground mt-1">
            Manage rooms that agents have joined but are not in the configuration
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={loadAgentRooms} disabled={loading}>
          <RefreshCw className={cn('h-4 w-4 mr-2', loading && 'animate-spin')} />
          Refresh
        </Button>
      </div>

      {/* Error Alert */}
      {error && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Summary Card */}
      {totalUnconfiguredRooms > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="text-base">Summary</CardTitle>
                <CardDescription>
                  {totalUnconfiguredRooms} external room
                  {totalUnconfiguredRooms !== 1 ? 's' : ''} found across{' '}
                  {agentsRooms.filter(a => a.unconfigured_rooms.length > 0).length} agent
                  {agentsRooms.filter(a => a.unconfigured_rooms.length > 0).length !== 1 ? 's' : ''}
                </CardDescription>
              </div>
              {selectedRooms.size > 0 && (
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={leaveSelectedRooms}
                  disabled={leavingRooms}
                >
                  <Trash2 className="h-4 w-4 mr-2" />
                  Leave {selectedRooms.size} Room{selectedRooms.size !== 1 ? 's' : ''}
                </Button>
              )}
            </div>
          </CardHeader>
        </Card>
      )}

      {/* No unconfigured rooms message */}
      {totalUnconfiguredRooms === 0 && (
        <Card>
          <CardContent className="pt-6">
            <p className="text-center text-muted-foreground">
              All agent rooms are properly configured. No action needed.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Agent Room Lists */}
      <ScrollArea className="h-[600px] pr-4">
        <div className="space-y-4">
          {agentsRooms
            .filter(agent => agent.unconfigured_rooms.length > 0)
            .map(agent => (
              <Card key={agent.agent_id}>
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <div>
                      <CardTitle className="text-lg">{agent.display_name}</CardTitle>
                      <CardDescription>
                        {agent.unconfigured_rooms.length} external room
                        {agent.unconfigured_rooms.length !== 1 ? 's' : ''}
                      </CardDescription>
                    </div>
                    <div className="flex gap-2">
                      {agent.unconfigured_rooms.length > 1 && (
                        <>
                          {areAllSelectedForAgent(agent.agent_id, agent.unconfigured_rooms) ? (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() =>
                                deselectAllForAgent(agent.agent_id, agent.unconfigured_rooms)
                              }
                            >
                              Deselect All
                            </Button>
                          ) : (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() =>
                                selectAllForAgent(agent.agent_id, agent.unconfigured_rooms)
                              }
                            >
                              Select All
                            </Button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2">
                    {agent.unconfigured_rooms.map((roomId, index) => {
                      const key = `${agent.agent_id}:${roomId}`;
                      const isSelected = selectedRooms.has(key);
                      const roomDetails = agent.unconfigured_room_details?.[index];

                      return (
                        <div
                          key={roomId}
                          className={cn(
                            'flex items-center space-x-3 p-3 rounded-lg border transition-colors cursor-pointer',
                            isSelected ? 'bg-muted/50 border-primary/20' : 'hover:bg-muted/30'
                          )}
                          onClick={() => toggleRoomSelection(agent.agent_id, roomId)}
                        >
                          <Checkbox
                            checked={isSelected}
                            onCheckedChange={() => toggleRoomSelection(agent.agent_id, roomId)}
                            disabled={leavingRooms}
                            onClick={e => e.stopPropagation()}
                          />
                          <div className="flex-1 min-w-0 select-none">
                            {/* Show room name if available */}
                            {roomDetails?.name && (
                              <div className="font-medium text-sm mb-1">{roomDetails.name}</div>
                            )}
                            <div className="flex items-center gap-2">
                              <code className="text-xs font-mono truncate text-muted-foreground">
                                {roomId}
                              </code>
                              {roomId.startsWith('!') && roomId.includes(':') && (
                                <Badge variant="outline" className="text-xs pointer-events-none">
                                  {roomId.split(':')[1]}
                                </Badge>
                              )}
                            </div>
                            {/* Show if it's a DM room */}
                            {roomId.includes('dm') && (
                              <p className="text-xs text-muted-foreground mt-1">
                                Direct Message Room
                              </p>
                            )}
                          </div>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={e => {
                              e.stopPropagation();
                              // Open room in Element/Matrix client
                              const matrixUrl = `https://matrix.to/#/${roomId}`;
                              window.open(matrixUrl, '_blank');
                            }}
                            title="Open in Matrix client"
                          >
                            <ExternalLink className="h-4 w-4" />
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                </CardContent>
              </Card>
            ))}
        </div>
      </ScrollArea>
    </div>
  );
}
