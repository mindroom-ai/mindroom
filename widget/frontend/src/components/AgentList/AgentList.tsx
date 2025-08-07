import { useConfigStore } from '@/store/configStore';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Plus, Bot } from 'lucide-react';
import { cn } from '@/lib/utils';

export function AgentList() {
  const { agents, selectedAgentId, selectAgent, createAgent } = useConfigStore();

  const handleCreateAgent = () => {
    const newAgent = {
      display_name: 'New Agent',
      role: 'A new agent that needs configuration',
      tools: [],
      instructions: [],
      rooms: ['lobby'],
      num_history_runs: 5,
    };
    createAgent(newAgent);
  };

  return (
    <Card className="h-full flex flex-col overflow-hidden">
      <CardHeader className="pb-3 flex-shrink-0">
        <div className="flex items-center justify-between">
          <CardTitle>Agents</CardTitle>
          <Button
            size="sm"
            onClick={handleCreateAgent}
            className="h-8 bg-gradient-to-r from-blue-600 to-purple-600 hover:from-blue-700 hover:to-purple-700 text-white shadow-sm"
          >
            <Plus className="h-4 w-4 mr-1" />
            Add
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-2 flex-1 overflow-y-auto min-h-0">
        <div className="space-y-1">
          {agents.map(agent => (
            <button
              key={agent.id}
              onClick={() => selectAgent(agent.id)}
              className={cn(
                'w-full text-left px-3 py-2 rounded-lg transition-all duration-200',
                'hover:bg-gray-100 hover:shadow-sm flex items-center gap-2',
                selectedAgentId === agent.id &&
                  'bg-gradient-to-r from-blue-50 to-purple-50 hover:from-blue-100 hover:to-purple-100 shadow-sm'
              )}
            >
              <Bot
                className={cn(
                  'h-4 w-4 transition-colors',
                  selectedAgentId === agent.id ? 'text-blue-600' : 'text-gray-500'
                )}
              />
              <div className="flex-1 min-w-0">
                <div className="font-medium text-sm">{agent.display_name}</div>
                <div className="text-xs text-gray-500 truncate">
                  {agent.tools.length} tools • {agent.rooms.length} rooms
                </div>
              </div>
            </button>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
