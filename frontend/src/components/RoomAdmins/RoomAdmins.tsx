import { useState } from "react";
import { ShieldCheck, X } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/components/ui/use-toast";
import { showSaveFailureToastIfNeeded } from "@/components/shared";
import { useConfigStore } from "@/store/configStore";

export function isConcreteMatrixUserId(userId: string): boolean {
  return (
    userId.startsWith("@") &&
    userId.includes(":") &&
    !userId.includes("*") &&
    !userId.includes("?") &&
    !userId.includes(" ")
  );
}

export function RoomAdmins() {
  const { config, isLoading, saveConfig, updateMatrixRoomAccess } =
    useConfigStore();
  const { toast } = useToast();
  const [newAdminId, setNewAdminId] = useState("");

  const roomAdmins = config?.matrix_room_access?.room_admins ?? [];

  const setRoomAdmins = (admins: string[]) => {
    updateMatrixRoomAccess({
      ...(config?.matrix_room_access ?? {}),
      room_admins: admins,
    });
  };

  const handleAdd = () => {
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
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-5 w-5 text-primary" />
          <CardTitle className="text-base">Room Admins</CardTitle>
        </div>
        <CardDescription>
          Matrix users automatically granted admin power (100) in every managed
          room. Membership is unchanged: listed users become admins once they
          are in the room.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
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
              disabled={!newAdminId.trim()}
            >
              Add
            </Button>
            <Button type="button" onClick={handleSave} disabled={isLoading}>
              Save
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
