import { Agent, Room, Team } from '@/types/config';

interface NetworkGraphProps {
  agents: Agent[];
  rooms: Room[];
  teams: Team[];
  selectedAgentId: string | null;
  selectedRoomId: string | null;
  onSelectAgent: (agentId: string) => void;
  onSelectRoom: (roomId: string) => void;
  width?: number;
  height?: number;
}

export function NetworkGraph({ agents, rooms, teams }: NetworkGraphProps) {
  // Calculate some basic statistics
  const totalConnections = agents.reduce((sum, agent) => sum + agent.rooms.length, 0);
  const averageToolsPerAgent =
    agents.length > 0
      ? agents.reduce((sum, agent) => sum + agent.tools.length, 0) / agents.length
      : 0;
  const teamMembership = teams.reduce((sum, team) => sum + team.agents.length, 0);

  return (
    <div className="w-full h-full bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <div className="h-full flex flex-col items-center justify-center space-y-4">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-white">System Overview</h3>

        <div className="grid grid-cols-2 gap-6 text-center">
          <div className="space-y-2">
            <div className="text-3xl font-bold text-blue-600 dark:text-blue-400">
              {agents.length}
            </div>
            <div className="text-sm text-gray-600 dark:text-gray-400">Agents</div>
          </div>

          <div className="space-y-2">
            <div className="text-3xl font-bold text-green-600 dark:text-green-400">
              {rooms.length}
            </div>
            <div className="text-sm text-gray-600 dark:text-gray-400">Rooms</div>
          </div>

          <div className="space-y-2">
            <div className="text-3xl font-bold text-purple-600 dark:text-purple-400">
              {teams.length}
            </div>
            <div className="text-sm text-gray-600 dark:text-gray-400">Teams</div>
          </div>

          <div className="space-y-2">
            <div className="text-3xl font-bold text-orange-600 dark:text-orange-400">
              {totalConnections}
            </div>
            <div className="text-sm text-gray-600 dark:text-gray-400">Connections</div>
          </div>
        </div>

        <div className="pt-4 border-t border-gray-200 dark:border-gray-700 space-y-2 text-center">
          <div className="text-sm text-gray-600 dark:text-gray-400">
            Average tools per agent:{' '}
            <span className="font-semibold">{averageToolsPerAgent.toFixed(1)}</span>
          </div>
          <div className="text-sm text-gray-600 dark:text-gray-400">
            Team memberships: <span className="font-semibold">{teamMembership}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
