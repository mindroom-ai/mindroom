"use client";

import {
  Lock,
  Users,
  Calendar,
  Database,
  BarChart3,
  MessageSquare,
  Mail,
  Search,
  Globe,
  FileText,
  PieChart,
  Table,
} from "lucide-react";
import { useEffect, useState } from "react";

// Types
type TabType = "agent" | "business" | "personal";
type BorderColor = "orange" | "blue" | "green" | "gray";

// Configuration
const BORDER_COLORS: Record<BorderColor, string> = {
  orange: "border-l-4 border-orange-400/60",
  blue: "border-l-4 border-blue-400/60",
  green: "border-l-4 border-green-400/60",
  gray: "border-l-4 border-gray-300/60 dark:border-gray-600/60",
};

const TAB_CONFIG: Record<
  TabType,
  {
    label: string;
    roomName: string;
    roomDescription: string;
    participantCount: number;
    participants: string[];
  }
> = {
  agent: {
    label: "Agent Team",
    roomName: "#competitor‑analysis",
    roomDescription: "Agent workspace · tools enabled",
    participantCount: 3,
    participants: ["you", "@researcher", "@analyst"],
  },
  business: {
    label: "Business",
    roomName: "#q4‑planning",
    roomDescription: "Federated room · org‑a.com ⇄ org‑b.net",
    participantCount: 4,
    participants: [
      "alice (org‑a)",
      "@mindroom_analyst",
      "bob (org‑b)",
      "@client_architect",
    ],
  },
  personal: {
    label: "Personal",
    roomName: "#weekend‑hike",
    roomDescription: "Encrypted room · friends",
    participantCount: 6,
    participants: [
      "alice",
      "bob",
      "carol",
      "@alice_calendar",
      "@bob_calendar",
      "@carol_calendar",
    ],
  },
};

// Helper functions
function getInitials(name: string): string {
  return name
    .replace(/[@]/g, "")
    .split(/\s|_/)
    .map((p) => p[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

function getBorderColor(isAgent: boolean, orgDomain?: string): string {
  if (isAgent) return BORDER_COLORS.orange;
  if (orgDomain?.includes("org-a")) return BORDER_COLORS.blue;
  if (orgDomain?.includes("org-b")) return BORDER_COLORS.green;
  return BORDER_COLORS.gray;
}

// Components
function Chip({
  label,
  icon,
  className = "",
}: {
  label: string;
  icon?: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded-full bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-600 ${className}`}
    >
      {icon}
      {label}
    </span>
  );
}

function Avatar({ name, color }: { name: string; color: string }) {
  const initials = getInitials(name);
  return (
    <div
      className={`w-6 h-6 md:w-8 md:h-8 rounded-full flex items-center justify-center text-white text-[10px] md:text-xs font-bold ${color}`}
    >
      {initials}
    </div>
  );
}

function TabButton({
  tab,
  currentTab,
  onClick,
}: {
  tab: TabType;
  currentTab: TabType;
  onClick: () => void;
}) {
  const isActive = tab === currentTab;
  const baseClass =
    "px-3 py-1.5 md:px-4 md:py-2 rounded-full text-sm font-medium border transition";
  const activeClass = "bg-orange-500 text-white border-orange-600 shadow";
  const inactiveClass =
    "bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-700 hover:border-orange-300 dark:hover:border-orange-700";

  return (
    <button
      onClick={onClick}
      className={`${baseClass} ${isActive ? activeClass : inactiveClass}`}
    >
      {TAB_CONFIG[tab].label}
    </button>
  );
}

function ChatBubble({
  side,
  name,
  org,
  text,
  chips = [],
  color,
  isAgent = false,
  orgDomain = "",
}: {
  side: "left" | "right";
  name: string;
  org: string;
  text: React.ReactNode;
  chips?: Array<{ label: string; icon?: React.ReactNode }>;
  color: string;
  isAgent?: boolean;
  orgDomain?: string;
}) {
  const borderAccent = getBorderColor(isAgent, orgDomain);

  // Simplified bubble styling logic
  const bubbleStyle =
    isAgent || side === "right"
      ? "bg-gray-50 dark:bg-gray-700 text-gray-900 dark:text-gray-100 border-gray-200 dark:border-gray-600"
      : "bg-white/90 dark:bg-gray-800/80 text-gray-800 dark:text-gray-200 border-gray-200 dark:border-gray-700";

  const alignment = side === "right" ? "justify-end" : "justify-start";
  const textAlign = side === "right" ? "items-end text-right" : "";

  return (
    <div className={`flex ${alignment}`}>
      {side === "left" && <Avatar name={name} color={color} />}
      <div className={`mx-2 max-w-[90%] ${textAlign}`}>
        <div className={`flex items-center gap-2 mb-1 ${alignment}`}>
          <span className="text-xs md:text-sm font-semibold text-gray-700 dark:text-gray-200">
            {name}
            {!isAgent && orgDomain && (
              <span className="font-normal text-gray-500">:{orgDomain}</span>
            )}
          </span>
          {isAgent && (
            <span className="text-[10px] px-1.5 py-0.5 bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400 rounded">
              AI Agent
            </span>
          )}
          {chips.map((chip, i) => (
            <Chip
              key={i}
              label={chip.label}
              icon={chip.icon}
              className="hidden md:inline-flex"
            />
          ))}
        </div>
        <div
          className={`rounded-2xl px-3 py-2 text-sm border ${borderAccent} ${bubbleStyle}`}
        >
          {text}
        </div>
      </div>
      {side === "right" && <Avatar name={name} color={color} />}
    </div>
  );
}

// Chat scenario components
function AgentTeamChat() {
  return (
    <div className="space-y-4">
      <div>
        <span className="text-gray-500 text-sm">You:</span>
        <div className="ml-2 mt-1 text-gray-700 dark:text-gray-300 text-sm">
          @researcher @analyst analyze our competitors and create a report
        </div>
      </div>

      <div className="pl-4 border-l-4 border-orange-400/60">
        <div className="text-orange-600 dark:text-orange-400 font-semibold text-sm">
          @researcher:
        </div>
        <div className="text-gray-600 dark:text-gray-400 text-sm mt-1">
          I'll gather data on your top 5 competitors...
        </div>
        <div className="text-xs text-gray-500 mt-2 bg-gray-100 dark:bg-gray-700/50 rounded px-2 py-1 inline-flex items-center gap-2">
          <span className="flex items-center gap-1">
            <Search className="w-3 h-3" />
            Web search
          </span>
          <span className="text-gray-400">·</span>
          <span className="flex items-center gap-1">
            <Database className="w-3 h-3" />
            Industry DB
          </span>
          <span className="text-gray-400">·</span>
          <span className="flex items-center gap-1">
            <Globe className="w-3 h-3" />
            News APIs
          </span>
        </div>
      </div>

      <div className="pl-4 border-l-4 border-blue-400/60">
        <div className="text-blue-600 dark:text-blue-400 font-semibold text-sm">
          @analyst:
        </div>
        <div className="text-gray-600 dark:text-gray-400 text-sm mt-1">
          I'll analyze market positioning and create visualizations...
        </div>
        <div className="text-xs text-gray-500 mt-2 bg-gray-100 dark:bg-gray-700/50 rounded px-2 py-1 inline-flex items-center gap-2">
          <span className="flex items-center gap-1">
            <BarChart3 className="w-3 h-3" />
            Analysis tools
          </span>
          <span className="text-gray-400">·</span>
          <span className="flex items-center gap-1">
            <PieChart className="w-3 h-3" />
            Charts
          </span>
          <span className="text-gray-400">·</span>
          <span className="flex items-center gap-1">
            <Table className="w-3 h-3" />
            Google Sheets
          </span>
        </div>
      </div>

      <div className="pl-4 border-l-4 border-green-400/60">
        <div className="text-green-600 dark:text-green-400 font-semibold text-sm">
          Together:
        </div>
        <div className="text-gray-600 dark:text-gray-400 text-sm mt-1">
          Report complete! Sent to your email and saved to Google Drive.
        </div>
        <div className="text-xs text-gray-500 mt-2 bg-gray-100 dark:bg-gray-700/50 rounded px-2 py-1 inline-flex items-center gap-2">
          <span className="flex items-center gap-1">
            <FileText className="w-3 h-3" />
            12 tools used
          </span>
          <span className="text-gray-400">·</span>
          <span className="flex items-center gap-1">
            <Database className="w-3 h-3" />
            847 sources
          </span>
          <span className="text-gray-400">·</span>
          <span>3 minutes</span>
        </div>
      </div>
    </div>
  );
}

function BusinessChat() {
  return (
    <>
      <ChatBubble
        side="left"
        name="alice"
        org="Matrix · org‑a.com"
        orgDomain="org-a.com"
        color="bg-indigo-500"
        text={
          <>
            @mindroom_analyst pull Q4 conversion vs target and propose actions
          </>
        }
      />
      <ChatBubble
        side="right"
        name="@mindroom_analyst"
        org="Matrix · agent"
        isAgent={true}
        color="bg-orange-500"
        chips={[
          { label: "DB", icon: <Database className="w-3 h-3" /> },
          { label: "Analytics", icon: <BarChart3 className="w-3 h-3" /> },
        ]}
        text={
          <>
            Fetching from DB + analytics… Chart attached. We're 13% below target
            on paid; suggest realloc + SEO refresh.
          </>
        }
      />
      <ChatBubble
        side="left"
        name="bob"
        org="Matrix · org‑b.net"
        orgDomain="org-b.net"
        color="bg-emerald-600"
        text={<>@client_architect is this compatible with our data model?</>}
      />
      <ChatBubble
        side="right"
        name="@client_architect"
        org="Matrix · agent"
        isAgent={true}
        color="bg-sky-600"
        text={
          <>Yes, schema v2 OK; can push PR to your repo when you approve.</>
        }
      />
      <ChatBubble
        side="left"
        name="alice"
        org="Matrix · org‑a.com"
        orgDomain="org-a.com"
        color="bg-indigo-500"
        text={
          <>
            Approved. @mindroom_analyst sync brief to Slack #marketing (via
            bridge).
          </>
        }
      />
      <ChatBubble
        side="right"
        name="@mindroom_analyst"
        org="Matrix · agent"
        isAgent={true}
        color="bg-orange-500"
        chips={[
          {
            label: "Slack bridge",
            icon: <MessageSquare className="w-3 h-3" />,
          },
        ]}
        text={<>Posted in Slack and invited @client_architect (read‑only).</>}
      />
    </>
  );
}

function PersonalChat() {
  return (
    <>
      <ChatBubble
        side="left"
        name="alice"
        org="Matrix"
        color="bg-indigo-500"
        text={<>Can we pick a weekend for the hike?</>}
      />
      <ChatBubble
        side="right"
        name="@alice_calendar"
        org="Matrix · agent"
        isAgent={true}
        color="bg-orange-500"
        chips={[{ label: "Calendar", icon: <Calendar className="w-3 h-3" /> }]}
        text={<>Checking weekends for Alice…</>}
      />
      <ChatBubble
        side="right"
        name="@bob_calendar"
        org="Matrix · agent"
        isAgent={true}
        color="bg-sky-600"
        chips={[{ label: "Calendar", icon: <Calendar className="w-3 h-3" /> }]}
        text={<>Bob is free Sat 14:00–18:00; busy Sunday morning.</>}
      />
      <ChatBubble
        side="right"
        name="@carol_calendar"
        org="Matrix · agent"
        isAgent={true}
        color="bg-emerald-600"
        chips={[{ label: "Calendar", icon: <Calendar className="w-3 h-3" /> }]}
        text={<>Carol is free Sunday 10:00–13:00; Sat is open after 17:00.</>}
      />
      <ChatBubble
        side="left"
        name="bob"
        org="Matrix"
        color="bg-emerald-600"
        text={<>Let's do Sunday 11:00 at the trailhead.</>}
      />
      <ChatBubble
        side="right"
        name="@alice_calendar"
        org="Matrix · agent"
        isAgent={true}
        color="bg-orange-500"
        chips={[
          { label: "Invites", icon: <Mail className="w-3 h-3" /> },
          {
            label: "Discord bridge",
            icon: <MessageSquare className="w-3 h-3" />,
          },
        ]}
        text={
          <>Invites sent and summary posted to Discord #friends (via bridge).</>
        }
      />
    </>
  );
}

export function Collaboration() {
  const [isVisible, setIsVisible] = useState(false);
  const [tab, setTab] = useState<TabType>("agent");

  useEffect(() => {
    const obs = new IntersectionObserver(
      ([entry]) => entry.isIntersecting && setIsVisible(true),
      { threshold: 0.1 },
    );
    const el = document.getElementById("collaboration");
    if (el) obs.observe(el);
    return () => el && obs.unobserve(el);
  }, []);

  const currentTabConfig = TAB_CONFIG[tab];

  return (
    <section
      id="collaboration"
      className="py-16 md:py-20 px-6 bg-gradient-to-b from-white to-gray-50 dark:from-gray-900 dark:to-gray-800"
    >
      <div className="container mx-auto max-w-6xl">
        <div className="text-center mb-12">
          <h2 className="text-3xl md:text-4xl font-bold bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Collaboration Scenarios
          </h2>
          <p className="text-gray-600 dark:text-gray-300">
            See how agents work together and collaborate across organizations
          </p>
        </div>

        {/* Tabs */}
        <div className="flex items-center justify-center gap-2 md:gap-3 mb-4 md:mb-6">
          {(["agent", "business", "personal"] as TabType[]).map((tabOption) => (
            <TabButton
              key={tabOption}
              tab={tabOption}
              currentTab={tab}
              onClick={() => setTab(tabOption)}
            />
          ))}
        </div>

        {/* Chat mock */}
        <div
          className={`mb-6 md:mb-8 ${isVisible ? "fade-in-up" : "opacity-0"}`}
        >
          <div className="rounded-2xl border border-gray-200 dark:border-gray-700 bg-white/80 dark:bg-gray-800/80 backdrop-blur shadow-lg overflow-hidden">
            {/* Chat header */}
            <div className="px-3 md:px-4 py-2.5 md:py-3 flex items-center justify-between border-b border-gray-200 dark:border-gray-700">
              <div className="flex items-center gap-2 md:gap-3">
                <Users className="w-4 h-4 text-gray-500" />
                <div>
                  <div className="text-sm font-semibold text-gray-800 dark:text-gray-200">
                    {currentTabConfig.roomName}
                  </div>
                  <div className="text-[11px] md:text-xs text-gray-500">
                    {currentTabConfig.roomDescription}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {/* Mobile: show compact participant summary */}
                <span className="text-xs text-gray-600 dark:text-gray-300 md:hidden">
                  {currentTabConfig.participantCount} participants
                </span>
                {/* Desktop: show participant chips */}
                <div className="hidden md:flex items-center gap-2 text-xs">
                  {currentTabConfig.participants.map((participant) => (
                    <Chip key={participant} label={participant} />
                  ))}
                </div>
                <Lock className="w-4 h-4 text-green-600" />
              </div>
            </div>

            {/* Chat content */}
            <div className="p-3 md:p-4 space-y-2 md:space-y-3">
              {tab === "agent" && <AgentTeamChat />}
              {tab === "business" && <BusinessChat />}
              {tab === "personal" && <PersonalChat />}
            </div>
          </div>
        </div>
        {/* Simple federation/bridge callout */}
        <div className="text-center mt-2 text-[13px] md:text-sm text-gray-600 dark:text-gray-300 max-w-3xl mx-auto">
          <p>
            <strong>Decentralized Federation:</strong> Like email or the web — no single point of control.
            Different organizations (org-a.com, org-b.net) collaborate in one encrypted room. Each
            participant is a real, verifiable Matrix account on their own
            server. The network survives even if servers disappear.
          </p>
          <p className="mt-1">
            <strong>Bridges:</strong> Connect existing tools (Slack, Discord,
            Telegram) to Matrix rooms. Bridge connections are not end-to-end
            encrypted.
          </p>
        </div>
      </div>
    </section>
  );
}
