import { useEffect, useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, useNavigate, useLocation } from 'react-router-dom';
import {
  BookOpen,
  Bot,
  Brain,
  Check,
  DoorOpen,
  Home,
  LayoutDashboard,
  Menu,
  Mic,
  Plug,
  Puzzle,
  Settings2,
  type LucideIcon,
  Users,
} from 'lucide-react';
import { useConfigStore } from '@/store/configStore';
import { AgentList } from '@/components/AgentList/AgentList';
import { AgentEditor } from '@/components/AgentEditor/AgentEditor';
import { TeamList } from '@/components/TeamList/TeamList';
import { TeamEditor } from '@/components/TeamEditor/TeamEditor';
import { RoomList } from '@/components/RoomList/RoomList';
import { RoomEditor } from '@/components/RoomEditor/RoomEditor';
import { ModelConfig } from '@/components/ModelConfig/ModelConfig';
import { MemoryConfig } from '@/components/MemoryConfig/MemoryConfig';
import { Knowledge } from '@/components/Knowledge/Knowledge';
import { VoiceConfig } from '@/components/VoiceConfig/VoiceConfig';
import { Integrations } from '@/components/Integrations/Integrations';
import { UnconfiguredRooms } from '@/components/UnconfiguredRooms/UnconfiguredRooms';
import { SyncStatus } from '@/components/SyncStatus/SyncStatus';
import { Dashboard } from '@/components/Dashboard/Dashboard';
import { Skills } from '@/components/Skills/Skills';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Toaster } from '@/components/ui/toaster';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { ThemeToggle } from '@/components/ThemeToggle/ThemeToggle';

const queryClient = new QueryClient();

type NavItem = {
  value: string;
  label: string;
  icon: LucideIcon;
  group: 'Workspace' | 'Configuration';
};

const NAV_ITEMS: NavItem[] = [
  { value: 'dashboard', label: 'Dashboard', icon: LayoutDashboard, group: 'Workspace' },
  { value: 'agents', label: 'Agents', icon: Bot, group: 'Workspace' },
  { value: 'teams', label: 'Teams', icon: Users, group: 'Workspace' },
  { value: 'rooms', label: 'Rooms', icon: Home, group: 'Workspace' },
  { value: 'unconfigured-rooms', label: 'External', icon: DoorOpen, group: 'Workspace' },
  { value: 'models', label: 'Models & API Keys', icon: Settings2, group: 'Configuration' },
  { value: 'memory', label: 'Memory', icon: Brain, group: 'Configuration' },
  { value: 'knowledge', label: 'Knowledge', icon: BookOpen, group: 'Configuration' },
  { value: 'voice', label: 'Voice', icon: Mic, group: 'Configuration' },
  { value: 'integrations', label: 'Tools', icon: Plug, group: 'Configuration' },
  { value: 'skills', label: 'Skills', icon: Puzzle, group: 'Configuration' },
];

const NAV_GROUPS: NavItem['group'][] = ['Workspace', 'Configuration'];

const TAB_TRIGGER_CLASS =
  'inline-flex items-center gap-1.5 rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap';

function AppContent() {
  const { loadConfig, syncStatus, error, selectedAgentId, selectedTeamId, selectedRoomId } =
    useConfigStore();
  const navigate = useNavigate();
  const location = useLocation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  // Get the current tab from URL or default to 'dashboard'
  const currentTab = location.pathname.slice(1) || 'dashboard';
  const currentNavItem = NAV_ITEMS.find(item => item.value === currentTab) || NAV_ITEMS[0];
  const CurrentNavIcon = currentNavItem.icon;

  useEffect(() => {
    // Load configuration on mount
    loadConfig();
  }, [loadConfig]);

  useEffect(() => {
    setMobileMenuOpen(false);
  }, [currentTab]);

  // Handle tab change - update the URL
  const handleTabChange = (value: string) => {
    navigate(`/${value}`);
  };

  const getPlatformUrl = () => {
    const configured = (import.meta as any).env?.VITE_PLATFORM_URL as string | undefined;
    if (configured && configured.length > 0) return configured;
    if (typeof window !== 'undefined') {
      const host = window.location.host;
      const firstDot = host.indexOf('.');
      const base = firstDot > 0 ? host.slice(firstDot + 1) : host; // 1.staging.mindroom.chat -> staging.mindroom.chat
      return `https://app.${base}`;
    }
    return 'https://app.mindroom.chat';
  };

  if (error) {
    const isAuthError =
      error.includes('Authentication required') || error.includes('Access denied');
    const isDifferentInstance = error.includes('Access denied');

    return (
      <div className="flex items-center justify-center h-screen bg-gradient-to-br from-amber-50 via-orange-50/40 to-yellow-50/50 dark:from-stone-950 dark:via-stone-900 dark:to-amber-950/20">
        <div className="max-w-md w-full mx-4 p-6 bg-white dark:bg-stone-900 rounded-lg shadow-lg">
          <div className="flex items-center mb-4">
            <span className="text-3xl mr-3">🔒</span>
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white">
              {isAuthError ? 'Access Required' : 'Configuration Error'}
            </h2>
          </div>
          <p className="text-gray-600 dark:text-gray-300 mb-6">{error}</p>

          {isAuthError && (
            <div className="space-y-3">
              {isDifferentInstance ? (
                <>
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    You are logged in but do not have access to this instance. You may need to:
                  </p>
                  <ul className="text-sm text-gray-500 dark:text-gray-400 list-disc ml-5 space-y-1">
                    <li>Switch to an instance you have access to</li>
                    <li>Request access from your administrator</li>
                    <li>Return to your dashboard</li>
                  </ul>
                  <a
                    href={`${getPlatformUrl()}/dashboard`}
                    className="block w-full text-center px-4 py-2 bg-primary text-white rounded-md hover:bg-primary/90 transition-colors"
                  >
                    Go to Dashboard
                  </a>
                </>
              ) : (
                <>
                  <p className="text-sm text-gray-500 dark:text-gray-400">
                    Please log in to access this MindRoom instance.
                  </p>
                  <a
                    href={`${getPlatformUrl()}/auth/login`}
                    className="block w-full text-center px-4 py-2 bg-primary text-white rounded-md hover:bg-primary/90 transition-colors"
                  >
                    Log In
                  </a>
                </>
              )}
            </div>
          )}

          {!isAuthError && (
            <div className="space-y-3">
              <button
                onClick={() => window.location.reload()}
                className="w-full px-4 py-2 bg-primary text-white rounded-md hover:bg-primary/90 transition-colors"
              >
                Retry
              </button>
              <p className="text-sm text-gray-500 dark:text-gray-400 text-center">
                If the problem persists, please contact support.
              </p>
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-screen relative overflow-hidden">
      {/* Warm gradient background layers */}
      <div className="absolute inset-0 bg-gradient-to-br from-amber-50 via-orange-50/40 to-yellow-50/50 dark:from-stone-950 dark:via-stone-900 dark:to-amber-950/20" />
      <div className="absolute inset-0 bg-gradient-to-tl from-orange-100/30 via-transparent to-amber-100/20 dark:from-amber-950/10 dark:via-transparent dark:to-orange-950/10" />
      <div className="absolute inset-0 gradient-mesh" />

      {/* Content wrapper */}
      <div className="relative z-10 flex flex-col h-full">
        {/* Header */}
        <header className="bg-white/80 dark:bg-stone-900/50 backdrop-blur-xl border-b border-gray-200/50 dark:border-white/10 shadow-sm dark:shadow-2xl">
          <div className="px-3 sm:px-6 py-2 sm:py-4 flex items-center justify-between gap-2">
            <h1 className="flex items-center gap-2 sm:gap-3">
              <span className="text-2xl sm:text-4xl">🧠</span>
              <div className="flex flex-col">
                <span className="text-base sm:text-3xl font-bold tracking-tight text-gray-900 dark:text-white">
                  MindRoom
                </span>
                <span className="hidden sm:block text-xs sm:text-sm font-normal text-gray-600 dark:text-gray-400 -mt-1">
                  Configuration
                </span>
              </div>
            </h1>

            <div className="flex items-center gap-1.5 sm:gap-4">
              <button
                type="button"
                onClick={() => setMobileMenuOpen(true)}
                aria-haspopup="dialog"
                aria-expanded={mobileMenuOpen}
                className="sm:hidden max-w-[8.5rem] rounded-lg border border-white/60 dark:border-white/10 bg-white/80 dark:bg-stone-900/70 backdrop-blur-xl px-2 py-1.5 flex items-center gap-1.5 min-w-0 text-left shadow-sm"
              >
                <CurrentNavIcon className="h-4 w-4 shrink-0 text-gray-700 dark:text-gray-200" />
                <span className="text-xs font-medium text-gray-900 dark:text-gray-100 truncate">
                  {currentNavItem.label}
                </span>
                <Menu className="h-4 w-4 shrink-0 text-gray-600 dark:text-gray-300" />
              </button>
              <ThemeToggle className="h-[30px] w-[30px] sm:h-9 sm:w-9 rounded-lg border-white/60 dark:border-white/10 bg-white/80 dark:bg-stone-900/70 backdrop-blur-xl shadow-sm hover:bg-white/90 dark:hover:bg-stone-900/80" />
              <SyncStatus status={syncStatus} compact className="sm:hidden" />
              <SyncStatus status={syncStatus} className="hidden sm:flex" />
            </div>
          </div>
        </header>

        {/* Main Content */}
        <div className="flex-1 overflow-hidden">
          <Tabs value={currentTab} onValueChange={handleTabChange} className="h-full flex flex-col">
            {/* Desktop Tab Navigation */}
            <TabsList className="hidden sm:flex px-3 sm:px-6 py-3 bg-white/70 dark:bg-stone-900/50 backdrop-blur-lg border-b border-gray-200/50 dark:border-white/10 flex-shrink-0 overflow-x-auto">
              {NAV_ITEMS.map(item => {
                const ItemIcon = item.icon;
                return (
                  <TabsTrigger key={item.value} value={item.value} className={TAB_TRIGGER_CLASS}>
                    <ItemIcon className="h-4 w-4" aria-hidden="true" />
                    <span>{item.label}</span>
                  </TabsTrigger>
                );
              })}
            </TabsList>

            <Dialog open={mobileMenuOpen} onOpenChange={setMobileMenuOpen}>
              <DialogContent className="w-[calc(100%-1.5rem)] max-w-sm p-0 border-white/60 dark:border-white/10 bg-white/95 dark:bg-stone-900/95 backdrop-blur-xl">
                <DialogHeader className="px-4 pt-4 pb-2 text-left">
                  <DialogTitle className="text-base text-gray-900 dark:text-gray-100">
                    Navigate
                  </DialogTitle>
                  <DialogDescription className="text-xs text-gray-600 dark:text-gray-400">
                    Choose a section
                  </DialogDescription>
                </DialogHeader>
                <div className="max-h-[70vh] overflow-y-auto px-2 pb-3">
                  {NAV_GROUPS.map(group => (
                    <div key={group} className="mb-3 last:mb-0">
                      <p className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                        {group}
                      </p>
                      <div className="space-y-1">
                        {NAV_ITEMS.filter(item => item.group === group).map(item => {
                          const isActive = item.value === currentTab;
                          const ItemIcon = item.icon;
                          return (
                            <button
                              key={item.value}
                              type="button"
                              onClick={() => handleTabChange(item.value)}
                              aria-current={isActive ? 'page' : undefined}
                              className={`w-full rounded-lg px-3 py-2 text-sm flex items-center justify-between transition-colors ${
                                isActive
                                  ? 'bg-primary/10 dark:bg-primary/20 text-primary'
                                  : 'text-gray-700 dark:text-gray-200 hover:bg-gray-100/80 dark:hover:bg-white/10'
                              }`}
                            >
                              <span className="flex items-center gap-2">
                                <ItemIcon className="h-4 w-4" aria-hidden="true" />
                                <span>{item.label}</span>
                              </span>
                              {isActive ? <Check className="h-4 w-4" /> : null}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  ))}
                </div>
              </DialogContent>
            </Dialog>

            <TabsContent value="dashboard" className="flex-1 p-2 sm:p-4 overflow-auto min-h-0">
              <div className="min-h-full">
                <Dashboard />
              </div>
            </TabsContent>

            <TabsContent value="agents" className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0">
              <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 sm:gap-4 h-full">
                <div
                  className={`col-span-1 lg:col-span-4 h-full overflow-hidden ${
                    selectedAgentId ? 'hidden lg:block' : 'block'
                  }`}
                >
                  <AgentList />
                </div>
                <div
                  className={`col-span-1 lg:col-span-8 h-full overflow-hidden ${
                    selectedAgentId ? 'block' : 'hidden lg:block'
                  }`}
                >
                  <AgentEditor />
                </div>
              </div>
            </TabsContent>

            <TabsContent value="teams" className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0">
              <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 sm:gap-4 h-full">
                <div
                  className={`col-span-1 lg:col-span-4 h-full overflow-hidden ${
                    selectedTeamId ? 'hidden lg:block' : 'block'
                  }`}
                >
                  <TeamList />
                </div>
                <div
                  className={`col-span-1 lg:col-span-8 h-full overflow-hidden ${
                    selectedTeamId ? 'block' : 'hidden lg:block'
                  }`}
                >
                  <TeamEditor />
                </div>
              </div>
            </TabsContent>

            <TabsContent value="rooms" className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0">
              <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 sm:gap-4 h-full">
                <div
                  className={`col-span-1 lg:col-span-4 h-full overflow-hidden ${
                    selectedRoomId ? 'hidden lg:block' : 'block'
                  }`}
                >
                  <RoomList />
                </div>
                <div
                  className={`col-span-1 lg:col-span-8 h-full overflow-hidden ${
                    selectedRoomId ? 'block' : 'hidden lg:block'
                  }`}
                >
                  <RoomEditor />
                </div>
              </div>
            </TabsContent>

            <TabsContent
              value="unconfigured-rooms"
              className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0"
            >
              <div className="h-full overflow-hidden">
                <UnconfiguredRooms />
              </div>
            </TabsContent>

            <TabsContent value="models" className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0">
              <div className="h-full overflow-hidden">
                <ModelConfig />
              </div>
            </TabsContent>

            <TabsContent value="memory" className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0">
              <div className="h-full overflow-hidden">
                <MemoryConfig />
              </div>
            </TabsContent>

            <TabsContent value="knowledge" className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0">
              <div className="h-full overflow-hidden">
                <Knowledge />
              </div>
            </TabsContent>

            <TabsContent value="voice" className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0">
              <div className="h-full overflow-auto">
                <VoiceConfig />
              </div>
            </TabsContent>

            <TabsContent value="integrations" className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0">
              <div className="h-full overflow-hidden">
                <Integrations />
              </div>
            </TabsContent>

            <TabsContent value="skills" className="flex-1 p-2 sm:p-4 overflow-hidden min-h-0">
              <div className="h-full overflow-hidden">
                <Skills />
              </div>
            </TabsContent>
          </Tabs>
        </div>

        <Toaster />
      </div>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <AppContent />
        </ThemeProvider>
      </QueryClientProvider>
    </BrowserRouter>
  );
}
