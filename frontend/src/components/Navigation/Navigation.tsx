import {
  BarChart3,
  BookOpen,
  Bot,
  Brain,
  CalendarClock,
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
  Wallet,
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
    value: "budgets",
    label: "Budgets",
    icon: Wallet,
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
    <div className="space-y-5">
      {NAV_GROUPS.map((group) => (
        <div key={group}>
          <p className="px-3 pb-1.5 text-xs text-muted-foreground">{group}</p>
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
                    "flex items-center gap-2.5 rounded-md px-3 text-sm text-muted-foreground outline-hidden transition-colors duration-150 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
                    mobile ? "min-h-11" : "min-h-8",
                    active && "bg-active font-medium text-foreground",
                  )}
                >
                  <ItemIcon aria-hidden="true" className="h-4 w-4 shrink-0" />
                  <span>{item.label}</span>
                </Link>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

/** The logo and wordmark that lead back to Home from the sidebar. */
function BrandLink() {
  return (
    <Link
      to="/dashboard"
      aria-label="MindRoom home"
      className="flex items-center gap-2.5 rounded-md outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
    >
      <img src="/logo.svg" alt="" className="h-7 w-7 shrink-0" />
      <span className="text-base font-semibold tracking-tight">MindRoom</span>
    </Link>
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
                "inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md transition-colors hover:bg-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring md:hidden",
                className,
              )}
            >
              <Menu aria-hidden="true" className="h-5 w-5" />
            </button>
          </DialogTrigger>
          <DialogContent className="left-0 top-0 h-dvh w-[min(18rem,85vw)] max-w-none translate-x-0 translate-y-0 content-start gap-3 overflow-y-auto rounded-none border-0 border-r bg-background p-3">
            <DialogHeader className="px-2 pb-1 pt-2 text-left">
              <DialogTitle className="text-base">Navigate</DialogTitle>
              <DialogDescription className="text-xs">
                MindRoom configuration sections
              </DialogDescription>
            </DialogHeader>
            <nav aria-label="Mobile navigation" className="px-3">
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
          aria-label="MindRoom"
          className={cn(
            "hidden w-56 shrink-0 flex-col border-r bg-background md:flex",
            className,
          )}
        >
          <div className="flex h-14 shrink-0 items-center border-b px-5">
            <BrandLink />
          </div>
          <nav
            aria-label="Primary navigation"
            className="min-h-0 flex-1 overflow-y-auto px-3 pb-6 pt-3"
          >
            <NavigationLinks currentValue={currentValue} />
          </nav>
        </aside>
      )}
    </>
  );
}
