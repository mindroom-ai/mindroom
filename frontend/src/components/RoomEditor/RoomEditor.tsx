import { useConfigStore } from "@/store/configStore";
import { SchemaSection } from "@/components/SchemaForm";
import { useSwipeBack } from "@/hooks/useSwipeBack";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Home, Bot } from "lucide-react";
import {
  EditorPanel,
  EditorPanelEmptyState,
  FieldGroup,
} from "@/components/shared";
import { Checkbox } from "@/components/ui/checkbox";
import type { Room } from "@/types/config";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

/** RoomConfig keys this editor renders by hand; More settings shows the rest. */
const ROOM_EDITOR_FIELDS = ["display_name", "description"] as const;

export function RoomEditor() {
  const {
    rooms,
    agents,
    config,
    selectedRoomId,
    updateRoom,
    deleteRoom,
    saveConfig,
    isDirty,
    isLoading,
    selectRoom,
    updateConfigValue,
  } = useConfigStore();

  const selectedRoom = rooms.find((r) => r.id === selectedRoomId);

  // Enable swipe back on mobile
  useSwipeBack({
    onSwipeBack: () => selectRoom(null),
    enabled: !!selectedRoomId && window.innerWidth < 1024,
  });

  if (!selectedRoom) {
    return (
      <EditorPanelEmptyState icon={Home} message="Select a room to edit" />
    );
  }

  const handleFieldChange = <K extends keyof Room>(field: K, value: Room[K]) =>
    updateRoom(selectedRoom.id, { [field]: value });

  const handleAgentToggle = (agentId: string, checked: boolean) =>
    handleFieldChange(
      "agents",
      checked
        ? [...selectedRoom.agents, agentId]
        : selectedRoom.agents.filter((id) => id !== agentId),
    );

  const handleDelete = () => {
    if (confirm("Are you sure you want to delete this room?")) {
      deleteRoom(selectedRoom.id);
    }
  };

  const modelOptions = Object.keys(config?.models || {});

  return (
    <EditorPanel
      icon={Home}
      title="Room Details"
      isDirty={isDirty}
      onSave={saveConfig}
      onDelete={handleDelete}
      disableSave={isLoading}
      onBack={() => selectRoom(null)}
    >
      {/* Display Name */}
      <FieldGroup
        label="Display Name"
        helperText="Human-readable name for the room"
        htmlFor="display-name"
      >
        <Input
          id="display-name"
          value={selectedRoom.display_name}
          onChange={(e) => handleFieldChange("display_name", e.target.value)}
          placeholder="Room name"
        />
      </FieldGroup>

      {/* Description */}
      <FieldGroup
        label="Description"
        helperText="Describe this room's purpose"
        htmlFor="description"
      >
        <Textarea
          id="description"
          value={selectedRoom.description || ""}
          onChange={(e) => handleFieldChange("description", e.target.value)}
          placeholder="Describe this room's purpose..."
          rows={3}
        />
      </FieldGroup>

      {/* Model Selection */}
      <FieldGroup
        label="Room Model (Optional)"
        helperText="Override the default model for agents and teams in this room"
        htmlFor="room-model"
      >
        <Select
          value={selectedRoom.model || "default_model"}
          onValueChange={(value) => {
            const newValue = value === "default_model" ? undefined : value;
            handleFieldChange("model", newValue);
          }}
        >
          <SelectTrigger id="room-model">
            <SelectValue placeholder="Select a model" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="default_model">Use default model</SelectItem>
            {modelOptions.map((modelId) => (
              <SelectItem key={modelId} value={modelId}>
                {modelId}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </FieldGroup>

      {/* Agents in Room */}
      <FieldGroup
        label="Agents in Room"
        helperText="Select agents that should have access to this room. Their room list will be updated automatically."
      >
        <div className="space-y-2 max-h-[300px] overflow-y-auto border rounded-lg p-2">
          {agents.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-4">
              No agents available
            </p>
          ) : (
            agents.map((agent) => (
              <div
                key={agent.id}
                className="flex items-center space-x-3 p-3 sm:p-2 rounded-lg hover:bg-secondary"
              >
                <Checkbox
                  id={`agent-${agent.id}`}
                  checked={selectedRoom.agents.includes(agent.id)}
                  onCheckedChange={(checked) =>
                    handleAgentToggle(agent.id, checked as boolean)
                  }
                  className="h-5 w-5 sm:h-4 sm:w-4"
                />
                <label
                  htmlFor={`agent-${agent.id}`}
                  className="flex-1 cursor-pointer select-none"
                >
                  <div className="flex items-center gap-2">
                    <Bot className="h-4 w-4 text-muted-foreground" />
                    <div>
                      <div className="font-medium text-sm">
                        {agent.display_name}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {agent.role}
                      </div>
                    </div>
                  </div>
                </label>
              </div>
            ))
          )}
        </div>
      </FieldGroup>

      <SchemaSection
        title="More settings"
        definition="RoomConfig"
        value={config?.rooms?.[selectedRoom.id]}
        path={["rooms", selectedRoom.id]}
        exclude={ROOM_EDITOR_FIELDS}
        onFieldChange={(key, next) =>
          updateConfigValue(["rooms", selectedRoom.id, key], next)
        }
      />
    </EditorPanel>
  );
}
