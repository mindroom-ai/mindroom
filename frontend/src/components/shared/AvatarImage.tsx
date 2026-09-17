import { useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

function AvatarContent({
  src,
  fallback,
}: {
  src: string;
  fallback: ReactNode;
}) {
  const [failed, setFailed] = useState(false);
  return failed ? (
    fallback
  ) : (
    <img
      src={src}
      alt=""
      loading="lazy"
      className="h-full w-full object-cover"
      onError={() => setFailed(true)}
    />
  );
}

/** Same-origin image presentation; a new source resets a previous load failure. */
export function AvatarImage({
  src,
  fallback,
  className,
}: {
  src: string;
  fallback: ReactNode;
  className?: string;
}) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-accent text-xs font-medium text-primary",
        className,
      )}
    >
      <AvatarContent key={src} src={src} fallback={fallback} />
    </span>
  );
}
