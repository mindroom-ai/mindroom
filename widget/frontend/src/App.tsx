import { useEffect } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useConfigStore } from '@/store/configStore';
import { AgentList } from '@/components/AgentList/AgentList';
import { AgentEditor } from '@/components/AgentEditor/AgentEditor';
import { TeamList } from '@/components/TeamList/TeamList';
import { TeamEditor } from '@/components/TeamEditor/TeamEditor';
import { ModelConfig } from '@/components/ModelConfig/ModelConfig';
import { SyncStatus } from '@/components/SyncStatus/SyncStatus';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Toaster } from '@/components/ui/toaster';

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
    <div className="flex flex-col h-screen bg-gradient-to-br from-gray-50 to-gray-100">
      {/* Header */}
      <header className="bg-gradient-to-r from-blue-600 to-purple-600 shadow-lg">
        <div className="px-6 py-4 flex items-center justify-between">
          <h1 className="text-2xl font-bold text-white">🧠 MindRoom Configuration</h1>
          <SyncStatus status={syncStatus} />
        </div>
      </header>

      {/* Main Content */}
      <div className="flex-1 overflow-hidden">
        <Tabs defaultValue="agents" className="h-full flex flex-col">
          <TabsList className="px-6 py-3 bg-white/80 backdrop-blur-sm border-b border-gray-200 flex-shrink-0">
            <TabsTrigger
              value="agents"
              className="data-[state=active]:bg-blue-50 data-[state=active]:text-blue-600"
            >
              👥 Agents
            </TabsTrigger>
            <TabsTrigger
              value="teams"
              className="data-[state=active]:bg-blue-50 data-[state=active]:text-blue-600"
            >
              👫 Teams
            </TabsTrigger>
            <TabsTrigger
              value="models"
              className="data-[state=active]:bg-blue-50 data-[state=active]:text-blue-600"
            >
              🔧 Models & API Keys
            </TabsTrigger>
          </TabsList>

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

          <TabsContent value="models" className="flex-1 p-4 overflow-hidden min-h-0">
            <div className="h-full overflow-hidden">
              <ModelConfig />
            </div>
          </TabsContent>
        </Tabs>
      </div>

      <Toaster />
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AppContent />
    </QueryClientProvider>
  );
}
