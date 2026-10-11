import { Hash, Users } from "lucide-react";
import { AvatarImage } from "./AvatarImage";

export function EntityAvatar({
  kind,
  id,
  name,
  className,
}: {
  kind: "agent" | "room" | "team";
  id: string;
  name: string;
  className?: string;
}) {
  const initials = name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => Array.from(part)[0])
    .join("")
    .toLocaleUpperCase();
  return (
    <AvatarImage
      src={
        kind === "room"
          ? `/api/matrix/rooms/avatar?room_id=${encodeURIComponent(id)}`
          : `/api/matrix/agents/${encodeURIComponent(id)}/avatar`
      }
      fallback={
        kind === "room" ? (
          <Hash className="h-1/2 w-1/2" />
        ) : kind === "team" ? (
          <Users className="h-1/2 w-1/2" />
        ) : (
          initials
        )
      }
      className={className}
    />
  );
}
