import dynamicIconImports from "lucide-react/dynamicIconImports";
import { createElement, lazy, type ReactNode } from "react";

// The package catalog includes aliases and loads only the requested icon.
const lucideIcons = new Map(
  Object.entries(dynamicIconImports).map(([name, load]) => [
    name.replace(/-/g, ""),
    lazy(load),
  ]),
);

export default function LucideIcon({
  name,
  className,
  fallback,
}: {
  name: string;
  className: string;
  fallback: ReactNode;
}) {
  const Icon = lucideIcons.get(
    name
      .replace(/^Lucide/, "")
      .replace(/Icon$/, "")
      .replace(/-/g, "")
      .toLowerCase(),
  );
  return Icon ? createElement(Icon, { className }) : fallback;
}
