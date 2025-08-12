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
          <Button size="sm" onClick={handleCreateAgent} className="h-8">
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
                'hover:bg-gray-100 dark:hover:bg-white/5 hover:shadow-sm hover:scale-[1.01] flex items-center gap-2 transition-all duration-200',
                selectedAgentId === agent.id &&
                  'bg-amber-50 dark:bg-gradient-to-r dark:from-primary/20 dark:to-primary/10 hover:bg-amber-100 dark:hover:from-primary/30 dark:hover:to-primary/20 shadow-sm dark:shadow-lg backdrop-blur-xl'
              )}
            >
              <Bot
                className={cn(
                  'h-4 w-4 transition-colors',
                  selectedAgentId === agent.id
                    ? 'text-primary dark:text-primary'
                    : 'text-gray-500 dark:text-gray-400'
                )}
              />
              <div className="flex-1 min-w-0">
                <div className="font-medium text-sm">{agent.display_name}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 truncate">
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
