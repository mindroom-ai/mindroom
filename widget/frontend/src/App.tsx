import React, { useEffect } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useConfigStore } from '@/store/configStore';
import { AgentList } from '@/components/AgentList/AgentList';
import { AgentEditor } from '@/components/AgentEditor/AgentEditor';
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
    <div className="flex flex-col h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white shadow-sm border-b">
        <div className="px-4 py-3 flex items-center justify-between">
          <h1 className="text-xl font-semibold text-gray-900">
            MindRoom Agent Configuration
          </h1>
          <SyncStatus status={syncStatus} />
        </div>
      </header>

      {/* Main Content */}
      <div className="flex-1 overflow-hidden">
        <Tabs defaultValue="agents" className="h-full">
          <TabsList className="px-4 py-2 bg-white border-b">
            <TabsTrigger value="agents">Agents</TabsTrigger>
            <TabsTrigger value="models">Models & API Keys</TabsTrigger>
          </TabsList>

          <TabsContent value="agents" className="h-full p-4">
            <div className="grid grid-cols-12 gap-4 h-full">
              <div className="col-span-4">
                <AgentList />
              </div>
              <div className="col-span-8">
                <AgentEditor />
              </div>
            </div>
          </TabsContent>

          <TabsContent value="models" className="h-full p-4">
            <ModelConfig />
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
