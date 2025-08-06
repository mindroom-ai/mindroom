import React from 'react';
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
    <Card className="h-full">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle>Agents</CardTitle>
          <Button
            variant="outline"
            size="sm"
            onClick={handleCreateAgent}
            className="h-8"
          >
            <Plus className="h-4 w-4 mr-1" />
            Add
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-2">
        <div className="space-y-1">
          {agents.map((agent) => (
            <button
              key={agent.id}
              onClick={() => selectAgent(agent.id)}
              className={cn(
                'w-full text-left px-3 py-2 rounded-md transition-colors',
                'hover:bg-gray-100 flex items-center gap-2',
                selectedAgentId === agent.id && 'bg-blue-50 hover:bg-blue-100'
              )}
            >
              <Bot className="h-4 w-4 text-gray-500" />
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
