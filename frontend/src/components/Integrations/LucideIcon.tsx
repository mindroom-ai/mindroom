import { icons } from "lucide-react";
import { createElement, type ReactNode } from "react";

const lucideIcons = new Map(Object.entries(icons));

export default function LucideIcon({
  name,
  className,
  fallback,
}: {
  name: string;
  className: string;
  fallback: ReactNode;
}) {
  const Icon =
    lucideIcons.get(name) ??
    lucideIcons.get(name.replace(/^Lucide/, "").replace(/Icon$/, ""));
  return Icon ? createElement(Icon, { className }) : fallback;
}
