import {
  BarChart3,
  BookOpen,
  Bot,
  Brain,
  CalendarClock,
  Check,
  DoorOpen,
  Home,
  KeyRound,
  LayoutDashboard,
  Menu,
  Mic,
  Plug,
  Puzzle,
  Settings2,
  SlidersHorizontal,
  type LucideIcon,
  Users,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

export type NavigationItem = {
  value: string;
  label: string;
  icon: LucideIcon;
  group: "Workspace" | "Configuration";
};

export const NAV_ITEMS: NavigationItem[] = [
  {
    value: "dashboard",
    label: "Dashboard",
    icon: LayoutDashboard,
    group: "Workspace",
  },
  { value: "agents", label: "Agents", icon: Bot, group: "Workspace" },
  { value: "teams", label: "Teams", icon: Users, group: "Workspace" },
  { value: "rooms", label: "Rooms", icon: Home, group: "Workspace" },
  {
    value: "schedules",
    label: "Schedules",
    icon: CalendarClock,
    group: "Workspace",
  },
  {
    value: "unconfigured-rooms",
    label: "External",
    icon: DoorOpen,
    group: "Workspace",
  },
  {
    value: "models",
    label: "Models",
    icon: Settings2,
    group: "Configuration",
  },
  {
    value: "usage",
    label: "Usage",
    icon: BarChart3,
    group: "Configuration",
  },
  {
    value: "memory",
    label: "Memory",
    icon: Brain,
    group: "Configuration",
  },
  {
    value: "knowledge",
    label: "Knowledge",
    icon: BookOpen,
    group: "Configuration",
  },
  {
    value: "credentials",
    label: "Credentials",
    icon: KeyRound,
    group: "Configuration",
  },
  { value: "voice", label: "Voice", icon: Mic, group: "Configuration" },
  { value: "integrations", label: "Tools", icon: Plug, group: "Configuration" },
  { value: "skills", label: "Skills", icon: Puzzle, group: "Configuration" },
  {
    value: "settings",
    label: "Settings",
    icon: SlidersHorizontal,
    group: "Configuration",
  },
];

const NAV_GROUPS: NavigationItem["group"][] = ["Workspace", "Configuration"];
const NAV_VALUES = new Set(NAV_ITEMS.map((item) => item.value));

export const DEFAULT_NAV_VALUE = NAV_ITEMS[0].value;

export function getNavigationValue(pathname: string): string {
  const [firstSegment] = pathname.split("/").filter(Boolean);
  return firstSegment && NAV_VALUES.has(firstSegment)
    ? firstSegment
    : DEFAULT_NAV_VALUE;
}

type NavigationProps = {
  mode?: "all" | "desktop" | "mobile";
  className?: string;
};

function NavigationLinks({
  currentValue,
  mobile = false,
  onNavigate,
}: {
  currentValue: string;
  mobile?: boolean;
  onNavigate?: () => void;
}) {
  return (
    <div className={cn("space-y-4", mobile && "space-y-3")}>
      {NAV_GROUPS.map((group) => (
        <div key={group}>
          <p className="px-3 pb-2.5 pt-1 text-[10px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
            {group}
          </p>
          <div className="space-y-0.5">
            {NAV_ITEMS.filter((item) => item.group === group).map((item) => {
              const active = item.value === currentValue;
              const ItemIcon = item.icon;
              return (
                <Link
                  key={item.value}
                  to={`/${item.value}`}
                  onClick={onNavigate}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "group flex items-center gap-2.5 rounded-md px-3 text-[13px] font-medium text-muted-foreground outline-none transition-colors hover:bg-foreground/[0.045] hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
                    mobile ? "min-h-11" : "min-h-[38px]",
                    active &&
                      "bg-primary/[0.12] text-primary hover:bg-primary/[0.16]",
                  )}
                >
                  <ItemIcon
                    aria-hidden="true"
                    className={cn(
                      "h-4 w-4 shrink-0 text-muted-foreground/80",
                      active && "text-primary",
                    )}
                  />
                  <span>{item.label}</span>
                  {active && (
                    <Check
                      aria-hidden="true"
                      className="ml-auto h-3.5 w-3.5 shrink-0"
                    />
                  )}
                </Link>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

export function Navigation({ mode = "all", className }: NavigationProps) {
  const location = useLocation();
  const [mobileOpen, setMobileOpen] = useState(false);
  const currentValue = getNavigationValue(location.pathname);

  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  return (
    <>
      {mode !== "desktop" && (
        <Dialog open={mobileOpen} onOpenChange={setMobileOpen}>
          <DialogTrigger asChild>
            <button
              type="button"
              aria-label="Open navigation"
              className={cn(
                "glass-control inline-flex h-11 w-11 shrink-0 items-center justify-center md:hidden",
                className,
              )}
            >
              <Menu aria-hidden="true" className="h-4 w-4" />
            </button>
          </DialogTrigger>
          <DialogContent className="glass-overlay left-3 top-3 h-[calc(100dvh-1.5rem)] w-[min(20rem,calc(100%-1.5rem))] max-w-none translate-x-0 translate-y-0 overflow-y-auto p-3 sm:rounded-xl">
            <DialogHeader className="px-2 pb-2 pt-1 text-left">
              <DialogTitle className="text-base">Navigate</DialogTitle>
              <DialogDescription className="text-xs">
                MindRoom configuration sections
              </DialogDescription>
            </DialogHeader>
            <nav aria-label="Mobile navigation">
              <NavigationLinks
                currentValue={currentValue}
                mobile
                onNavigate={() => setMobileOpen(false)}
              />
            </nav>
          </DialogContent>
        </Dialog>
      )}

      {mode !== "mobile" && (
        <aside
          className={cn(
            "glass-panel shell-navigation mb-4 ml-4 hidden w-52 shrink-0 overflow-y-auto rounded-xl px-3 py-4 md:block",
            className,
          )}
        >
          <nav aria-label="Primary navigation">
            <NavigationLinks currentValue={currentValue} />
          </nav>
        </aside>
      )}
    </>
  );
}
