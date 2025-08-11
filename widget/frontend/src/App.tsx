import { useEffect } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useConfigStore } from '@/store/configStore';
import { AgentList } from '@/components/AgentList/AgentList';
import { AgentEditor } from '@/components/AgentEditor/AgentEditor';
import { TeamList } from '@/components/TeamList/TeamList';
import { TeamEditor } from '@/components/TeamEditor/TeamEditor';
import { RoomList } from '@/components/RoomList/RoomList';
import { RoomEditor } from '@/components/RoomEditor/RoomEditor';
import { ModelConfig } from '@/components/ModelConfig/ModelConfig';
import { MemoryConfig } from '@/components/MemoryConfig/MemoryConfig';
import { Integrations } from '@/components/Integrations/Integrations';
import { SyncStatus } from '@/components/SyncStatus/SyncStatus';
import { Dashboard } from '@/components/Dashboard/Dashboard';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Toaster } from '@/components/ui/toaster';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { ThemeToggle } from '@/components/ThemeToggle/ThemeToggle';

const queryClient = new QueryClient();

function AppContent() {
  const { loadConfig, syncStatus, error } = useConfigStore();

  useEffect(() => {
    // Load configuration on mount
    loadConfig();
  }, [loadConfig]);

  if (error) {
    return (
      <div className="flex items-center justify-center h-screen">
        <div className="text-red-600">
          <h2 className="text-xl font-semibold">Error Loading Configuration</h2>
          <p>{error}</p>
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
          <div className="px-6 py-4 flex items-center justify-between">
            <h1 className="flex items-center gap-3">
              <span className="text-4xl">🧠</span>
              <div className="flex flex-col">
                <span className="text-3xl font-bold tracking-tight text-gray-900 dark:text-white">
                  MindRoom
                </span>
                <span className="text-sm font-normal text-gray-600 dark:text-gray-400 -mt-1">
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
          <Tabs defaultValue="dashboard" className="h-full flex flex-col">
            <TabsList className="px-6 py-3 bg-white/70 dark:bg-stone-900/50 backdrop-blur-lg border-b border-gray-200/50 dark:border-white/10 flex-shrink-0">
              <TabsTrigger
                value="dashboard"
                className="data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all"
              >
                📊 Dashboard
              </TabsTrigger>
              <TabsTrigger
                value="agents"
                className="data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all"
              >
                👥 Agents
              </TabsTrigger>
              <TabsTrigger
                value="teams"
                className="data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all"
              >
                👫 Teams
              </TabsTrigger>
              <TabsTrigger
                value="rooms"
                className="data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all"
              >
                🏠 Rooms
              </TabsTrigger>
              <TabsTrigger
                value="models"
                className="data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all"
              >
                🔧 Models & API Keys
              </TabsTrigger>
              <TabsTrigger
                value="memory"
                className="data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all"
              >
                🧠 Memory
              </TabsTrigger>
              <TabsTrigger
                value="integrations"
                className="data-[state=active]:bg-white/50 dark:data-[state=active]:bg-primary/20 data-[state=active]:text-primary data-[state=active]:shadow-sm data-[state=active]:backdrop-blur-xl data-[state=active]:border data-[state=active]:border-white/50 dark:data-[state=active]:border-primary/30 transition-all"
              >
                🔌 Integrations
              </TabsTrigger>
            </TabsList>

            <TabsContent value="dashboard" className="flex-1 p-4 overflow-hidden min-h-0">
              <div className="h-full overflow-hidden">
                <Dashboard />
              </div>
            </TabsContent>

            <TabsContent value="agents" className="flex-1 p-4 overflow-hidden min-h-0">
              <div className="grid grid-cols-12 gap-4 h-full">
                <div className="col-span-4 h-full overflow-hidden">
                  <AgentList />
                </div>
                <div className="col-span-8 h-full overflow-hidden">
                  <AgentEditor />
                </div>
              </div>
            </TabsContent>

            <TabsContent value="teams" className="flex-1 p-4 overflow-hidden min-h-0">
              <div className="grid grid-cols-12 gap-4 h-full">
                <div className="col-span-4 h-full overflow-hidden">
                  <TeamList />
                </div>
                <div className="col-span-8 h-full overflow-hidden">
                  <TeamEditor />
                </div>
              </div>
            </TabsContent>

            <TabsContent value="rooms" className="flex-1 p-4 overflow-hidden min-h-0">
              <div className="grid grid-cols-12 gap-4 h-full">
                <div className="col-span-4 h-full overflow-hidden">
                  <RoomList />
                </div>
                <div className="col-span-8 h-full overflow-hidden">
                  <RoomEditor />
                </div>
              </div>
            </TabsContent>

            <TabsContent value="models" className="flex-1 p-4 overflow-hidden min-h-0">
              <div className="h-full overflow-hidden">
                <ModelConfig />
              </div>
            </TabsContent>

            <TabsContent value="memory" className="flex-1 p-4 overflow-hidden min-h-0">
              <div className="h-full overflow-hidden">
                <MemoryConfig />
              </div>
            </TabsContent>

            <TabsContent value="integrations" className="flex-1 p-4 overflow-hidden min-h-0">
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
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <AppContent />
      </ThemeProvider>
    </QueryClientProvider>
  );
}
