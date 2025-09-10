import { useEffect } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, useNavigate, useLocation } from 'react-router-dom';
import { useConfigStore } from '@/store/configStore';
import { AgentList } from '@/components/AgentList/AgentList';
import { AgentEditor } from '@/components/AgentEditor/AgentEditor';
import { TeamList } from '@/components/TeamList/TeamList';
import { TeamEditor } from '@/components/TeamEditor/TeamEditor';
import { RoomList } from '@/components/RoomList/RoomList';
import { RoomEditor } from '@/components/RoomEditor/RoomEditor';
import { ModelConfig } from '@/components/ModelConfig/ModelConfig';
import { MemoryConfig } from '@/components/MemoryConfig/MemoryConfig';
import { VoiceConfig } from '@/components/VoiceConfig/VoiceConfig';
import { Integrations } from '@/components/Integrations/Integrations';
import { UnconfiguredRooms } from '@/components/UnconfiguredRooms/UnconfiguredRooms';
import { SyncStatus } from '@/components/SyncStatus/SyncStatus';
import { Dashboard } from '@/components/Dashboard/Dashboard';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Toaster } from '@/components/ui/toaster';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { ThemeToggle } from '@/components/ThemeToggle/ThemeToggle';

const queryClient = new QueryClient();

function AppContent() {
  const { loadConfig, syncStatus, error, selectedAgentId, selectedTeamId, selectedRoomId } =
    useConfigStore();
  const navigate = useNavigate();
  const location = useLocation();

  // Get the current tab from URL or default to 'dashboard'
  const currentTab = location.pathname.slice(1) || 'dashboard';

  useEffect(() => {
    // Load configuration on mount
    loadConfig();
  }, [loadConfig]);

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
                    You are logged in but don't have access to this instance. You may need to:
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
          <div className="px-3 sm:px-6 py-3 sm:py-4 flex items-center justify-between">
            <h1 className="flex items-center gap-2 sm:gap-3">
              <span className="text-3xl sm:text-4xl">🧠</span>
              <div className="flex flex-col">
                <span className="text-2xl sm:text-3xl font-bold tracking-tight text-gray-900 dark:text-white">
                  MindRoom
                </span>
                <span className="text-xs sm:text-sm font-normal text-gray-600 dark:text-gray-400 -mt-1">
                  Configuration
                </span>
              </div>
            </h1>
            <div className="flex items-center gap-4">
              <ThemeToggle />
              <SyncStatus status={syncStatus} />
            </div>
          </div>
        </header>

        {/* Main Content */}
        <div className="flex-1 overflow-hidden">
          <Tabs value={currentTab} onValueChange={handleTabChange} className="h-full flex flex-col">
            {/* Tab Navigation */}
            <TabsList className="px-3 sm:px-6 py-3 bg-white/70 dark:bg-stone-900/50 backdrop-blur-lg border-b border-gray-200/50 dark:border-white/10 flex-shrink-0 overflow-x-auto">
              <TabsTrigger
                value="dashboard"
                className="rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap"
              >
                📊 Dashboard
              </TabsTrigger>
              <TabsTrigger
                value="agents"
                className="rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap"
              >
                👥 Agents
              </TabsTrigger>
              <TabsTrigger
                value="teams"
                className="rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap"
              >
                👫 Teams
              </TabsTrigger>
              <TabsTrigger
                value="rooms"
                className="rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap"
              >
                🏠 Rooms
              </TabsTrigger>
              <TabsTrigger
                value="unconfigured-rooms"
                className="rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap"
              >
                🚪 External
              </TabsTrigger>
              <TabsTrigger
                value="models"
                className="rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap"
              >
                🔧 Models & API Keys
              </TabsTrigger>
              <TabsTrigger
                value="memory"
                className="rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap"
              >
                🧠 Memory
              </TabsTrigger>
              <TabsTrigger
                value="voice"
                className="rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap"
              >
                🎤 Voice
              </TabsTrigger>
              <TabsTrigger
                value="integrations"
                className="rounded-lg data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all whitespace-nowrap"
              >
                🔌 Integrations
              </TabsTrigger>
            </TabsList>

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
