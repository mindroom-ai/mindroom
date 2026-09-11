import { Plug } from "lucide-react";
import { iconMap } from "@/components/Integrations/iconMapping";

const icons = Object.entries(iconMap)
  .map(([name, Icon]) => ({
    name: name
      .replace(/^(Fa|Fi|Si|Gi|Tb|Vsc|Wi|Aws)(?=[A-Z])/, "")
      .toLowerCase(),
    Icon,
  }))
  .sort((a, b) => b.name.length - a.name.length);

// Ignore casing and separators, allowing names such as "Google Calendar MCP".
function nameVariants(name: string): string[] {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
  const variants: string[] = [];
  for (let start = 0; start < words.length; start++) {
    let variant = "";
    for (const word of words.slice(start)) {
      variant += word;
      variants.push(variant);
    }
  }
  return variants;
}

export function ConnectionIcon({
  names,
  iconName,
}: {
  names: string[];
  iconName?: string | null;
}) {
  const variants = new Set(names.flatMap(nameVariants));
  // Prefer specific names (Google Calendar) over generic ones (Calendar).
  const Icon =
    (iconName ? iconMap[iconName] : undefined) ??
    icons.find(({ name }) => variants.has(name))?.Icon ??
    Plug;

  return (
    <span
      aria-hidden="true"
      className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-muted/60 text-primary ring-1 ring-inset ring-border/60"
    >
      <Icon className="h-5 w-5" />
    </span>
  );
}
