import { useState, useEffect } from "react";
import { Trash2, RefreshCw, ExternalLink, AlertCircle } from "lucide-react";
import { API_ENDPOINTS, fetchJSON } from "../../lib/api";
import { Button } from "../ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../ui/card";
import { Checkbox } from "../ui/checkbox";
import { Alert, AlertDescription } from "../ui/alert";
import { cn } from "../../lib/utils";

interface RoomInfo {
  room_id: string;
  name?: string;
}

interface MatrixEntityRoomsInfo {
  agent_id: string; // Legacy API field; contains either an agent ID or team ID.
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

interface AgentRoomsResponse {
  agents: MatrixEntityRoomsInfo[];
}

interface LeaveRoomsBulkResponse {
  success: boolean;
  results: Array<{ success: boolean }>;
}

export function UnconfiguredRooms() {
  const [entitiesRooms, setEntitiesRooms] = useState<MatrixEntityRoomsInfo[]>(
    [],
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedRooms, setSelectedRooms] = useState<Set<string>>(new Set());
  const [leavingRooms, setLeavingRooms] = useState(false);

  // Load configured Matrix entity rooms data.
  const loadAgentRooms = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJSON<AgentRoomsResponse>(
        API_ENDPOINTS.matrix.agentsRooms,
      );
      setEntitiesRooms(response.agents);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load external rooms",
      );
      console.error("Error loading external rooms:", err);
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
    setSelectedRooms((prev) => {
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
    setSelectedRooms((prev) => {
      const newSet = new Set(prev);
      rooms.forEach((roomId) => {
        newSet.add(`${agentId}:${roomId}`);
      });
      return newSet;
    });
  };

  // Deselect all unconfigured rooms for an agent
  const deselectAllForAgent = (agentId: string, rooms: string[]) => {
    setSelectedRooms((prev) => {
      const newSet = new Set(prev);
      rooms.forEach((roomId) => {
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
      const requests: RoomLeaveRequest[] = Array.from(selectedRooms).map(
        (key) => {
          // Split only on the first colon to preserve the room ID format (!room:server)
          const colonIndex = key.indexOf(":");
          const agent_id = key.substring(0, colonIndex);
          const room_id = key.substring(colonIndex + 1);
          return { agent_id, room_id };
        },
      );

      const response = await fetchJSON<LeaveRoomsBulkResponse>(
        API_ENDPOINTS.matrix.leaveRoomsBulk,
        {
          method: "POST",
          body: JSON.stringify(requests),
        },
      );

      if (response.success) {
        // Clear selection and reload data
        setSelectedRooms(new Set());
        await loadAgentRooms();
      } else {
        // Handle partial failures
        const failed = response.results.filter((result) => !result.success);
        if (failed.length > 0) {
          setError(
            `Failed to leave ${failed.length} room(s). Please try again.`,
          );
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to leave rooms");
      console.error("Error leaving rooms:", err);
    } finally {
      setLeavingRooms(false);
    }
  };

  // Calculate total unconfigured rooms
  const totalUnconfiguredRooms = entitiesRooms.reduce(
    (sum, entity) => sum + entity.unconfigured_rooms.length,
    0,
  );
  const entitiesWithExternalRooms = entitiesRooms.filter(
    (entity) => entity.unconfigured_rooms.length > 0,
  );

  // Check if all rooms for an entity are selected.
  const areAllSelectedForAgent = (agentId: string, rooms: string[]) => {
    return rooms.every((roomId) => selectedRooms.has(`${agentId}:${roomId}`));
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
    <div className="min-w-0 space-y-4">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-2xl font-semibold tracking-tight">
            External Rooms
          </h2>
          <Button
            variant="outline"
            size="sm"
            onClick={loadAgentRooms}
            className="shrink-0"
            disabled={loading}
          >
            <RefreshCw
              className={cn("h-4 w-4 mr-2", loading && "animate-spin")}
            />
            Refresh
          </Button>
        </div>
        <p className="text-sm text-muted-foreground">
          Manage rooms that agents and teams have joined but are not in the
          configuration
        </p>
      </div>

      {/* Error Alert */}
      {error && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {totalUnconfiguredRooms > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground">
            {totalUnconfiguredRooms} external room
            {totalUnconfiguredRooms !== 1 ? "s" : ""} found across{" "}
            {entitiesWithExternalRooms.length} entit
            {entitiesWithExternalRooms.length === 1 ? "y" : "ies"}
          </p>
          {selectedRooms.size > 0 && (
            <Button
              variant="destructive"
              size="sm"
              onClick={leaveSelectedRooms}
              disabled={leavingRooms}
            >
              <Trash2 className="h-4 w-4 mr-2" />
              Leave {selectedRooms.size} Room
              {selectedRooms.size !== 1 ? "s" : ""}
            </Button>
          )}
        </div>
      )}

      {/* No unconfigured rooms message */}
      {totalUnconfiguredRooms === 0 && (
        <Card>
          <CardContent className="pt-6">
            <p className="text-center text-muted-foreground">
              All configured agents and teams are only in configured rooms. No
              action needed.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Entity Room Lists */}
      <div className="space-y-4">
        {entitiesWithExternalRooms.map((entity) => (
          <Card key={entity.agent_id} className="min-w-0">
            <CardHeader>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1 basis-40">
                  <CardTitle className="break-words text-lg">
                    {entity.display_name}
                  </CardTitle>
                  <CardDescription>
                    {entity.unconfigured_rooms.length} external room
                    {entity.unconfigured_rooms.length !== 1 ? "s" : ""}
                  </CardDescription>
                </div>
                <div className="flex shrink-0 gap-2">
                  {entity.unconfigured_rooms.length > 1 && (
                    <>
                      {areAllSelectedForAgent(
                        entity.agent_id,
                        entity.unconfigured_rooms,
                      ) ? (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() =>
                            deselectAllForAgent(
                              entity.agent_id,
                              entity.unconfigured_rooms,
                            )
                          }
                        >
                          Deselect All
                        </Button>
                      ) : (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() =>
                            selectAllForAgent(
                              entity.agent_id,
                              entity.unconfigured_rooms,
                            )
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
                {entity.unconfigured_rooms.map((roomId, index) => {
                  const key = `${entity.agent_id}:${roomId}`;
                  const isSelected = selectedRooms.has(key);
                  const roomDetails = entity.unconfigured_room_details?.[index];
                  const roomLabel = roomDetails?.name
                    ? `${roomDetails.name} (${roomId})`
                    : roomId;

                  return (
                    <div
                      key={roomId}
                      className={cn(
                        "flex min-w-0 items-center gap-3 rounded-lg px-2 py-3 transition-colors cursor-pointer",
                        isSelected ? "bg-primary/10" : "hover:bg-muted/30",
                      )}
                      onClick={() =>
                        toggleRoomSelection(entity.agent_id, roomId)
                      }
                    >
                      <Checkbox
                        checked={isSelected}
                        aria-label={`Select ${roomLabel} for ${entity.display_name}`}
                        onCheckedChange={() =>
                          toggleRoomSelection(entity.agent_id, roomId)
                        }
                        disabled={leavingRooms}
                        onClick={(e) => e.stopPropagation()}
                      />
                      <div className="flex-1 min-w-0 select-none">
                        {/* Show room name if available */}
                        {roomDetails?.name && (
                          <div className="break-words font-medium text-sm mb-1">
                            {roomDetails.name}
                          </div>
                        )}
                        <code className="block break-all text-xs font-mono text-muted-foreground">
                          {roomId}
                        </code>
                        {/* Show if it's a DM room */}
                        {roomId.includes("dm") && (
                          <p className="text-xs text-muted-foreground mt-1">
                            Direct Message Room
                          </p>
                        )}
                      </div>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-11 w-11 shrink-0"
                        aria-label={`Open ${roomLabel} in Matrix client`}
                        onClick={(e) => {
                          e.stopPropagation();
                          // Open room in Element/Matrix client
                          const matrixUrl = `https://matrix.to/#/${roomId}`;
                          window.open(matrixUrl, "_blank");
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
    </div>
  );
}
