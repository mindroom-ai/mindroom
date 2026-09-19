import { useState } from "react";
import { ChevronDown, ShieldCheck, X } from "lucide-react";
import { Card, CardContent, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/components/ui/use-toast";
import { showSaveFailureToastIfNeeded } from "@/components/shared";
import { useConfigStore } from "@/store/configStore";
import { isConcreteMatrixUserId } from "@/lib/matrixIds";

export function RoomAdmins() {
  const { config, isLoading, isDirty, saveConfig, updateRoomDefaults } =
    useConfigStore();
  const { toast } = useToast();
  const [newAdminId, setNewAdminId] = useState("");
  const [isExpanded, setIsExpanded] = useState(false);

  const roomAdmins = config?.room_defaults?.admins ?? [];
  const adminCountLabel = `${roomAdmins.length} ${roomAdmins.length === 1 ? "admin" : "admins"}`;

  const setRoomAdmins = (admins: string[]) => {
    updateRoomDefaults({
      ...(config?.room_defaults ?? {}),
      admins,
    });
  };

  const handleAdd = () => {
    if (!config) return;
    const trimmed = newAdminId.trim();
    if (!trimmed) return;
    if (!isConcreteMatrixUserId(trimmed)) {
      toast({
        title: "Invalid Matrix user ID",
        description:
          "Use a full Matrix user ID like @alice:example.com (no wildcards).",
        variant: "destructive",
      });
      return;
    }
    if (roomAdmins.includes(trimmed)) {
      toast({
        title: "Already a room admin",
        description: `${trimmed} is already in the room admins list.`,
      });
      return;
    }
    setRoomAdmins([...roomAdmins, trimmed]);
    setNewAdminId("");
  };

  const handleRemove = (userId: string) => {
    setRoomAdmins(roomAdmins.filter((admin) => admin !== userId));
  };

  const handleSave = async () => {
    const result = await saveConfig();
    if (
      showSaveFailureToastIfNeeded(result, {
        staleMessage: "Save was superseded by newer room admin edits.",
        fallbackMessage: "Failed to save room admins.",
      })
    ) {
      return;
    }
    toast({
      title: "Room Admins Saved",
      description:
        "Listed users get admin power in every managed room once they are in it.",
    });
  };

  return (
    <Card>
      <button
        type="button"
        aria-expanded={isExpanded}
        aria-controls="room-admins-content"
        aria-label={`Room Admins, ${adminCountLabel}`}
        onClick={() => setIsExpanded((expanded) => !expanded)}
        className="flex min-h-12 w-full items-center justify-between gap-3 rounded-lg px-4 py-3 text-left transition-colors hover:bg-foreground/[0.035] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      >
        <span className="flex items-center gap-2">
          <ShieldCheck className="h-5 w-5 text-primary" />
          <span className="text-base font-semibold">Room Admins</span>
        </span>
        <span className="flex items-center gap-2 text-sm text-muted-foreground">
          {adminCountLabel}
          <ChevronDown
            aria-hidden="true"
            className={`h-4 w-4 transition-transform ${isExpanded ? "rotate-180" : ""}`}
          />
        </span>
      </button>
      {isExpanded && (
        <CardContent id="room-admins-content" className="space-y-3 pt-0">
          <CardDescription>
            Matrix users automatically granted admin power (100) in every
            managed room. Membership is unchanged: listed users become admins
            once they are in the room. Removing a user stops future grants but
            does not lower admin power they already have.
          </CardDescription>
          {roomAdmins.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {roomAdmins.map((userId) => (
                <Badge
                  key={userId}
                  variant="secondary"
                  className="flex items-center gap-1 font-mono"
                >
                  {userId}
                  <button
                    type="button"
                    aria-label={`Remove ${userId}`}
                    onClick={() => handleRemove(userId)}
                    className="ml-1 rounded-full hover:text-destructive"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              ))}
            </div>
          )}
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              value={newAdminId}
              onChange={(e) => setNewAdminId(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  handleAdd();
                }
              }}
              placeholder="@alice:example.com"
              className="font-mono sm:max-w-sm"
            />
            <div className="flex gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={handleAdd}
                disabled={!newAdminId.trim() || !config}
              >
                Add
              </Button>
              <Button
                type="button"
                onClick={handleSave}
                disabled={isLoading || !config || !isDirty}
              >
                Save
              </Button>
            </div>
          </div>
        </CardContent>
      )}
    </Card>
  );
}
