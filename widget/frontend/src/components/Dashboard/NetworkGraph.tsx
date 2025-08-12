import { Agent, Room, Team } from '@/types/config';
import { Bot, Home, Users, Link, Activity, Trophy, Zap, BarChart3, X } from 'lucide-react';
import { getSelectionStyles } from '@/components/shared/styles';

interface NetworkGraphProps {
  agents: Agent[];
  rooms: Room[];
  teams: Team[];
  selectedAgentId: string | null;
  selectedRoomId: string | null;
  onSelectAgent: (agentId: string | null) => void;
  onSelectRoom: (roomId: string | null) => void;
  width?: number;
  height?: number;
}

export function NetworkGraph({
  agents,
  rooms,
  teams,
  selectedAgentId,
  selectedRoomId,
  onSelectAgent,
  onSelectRoom,
}: NetworkGraphProps) {
  // Calculate relationship stats
  const totalConnections = agents.reduce((sum, agent) => sum + agent.rooms.length, 0);
  const averageToolsPerAgent =
    agents.length > 0
      ? agents.reduce((sum, agent) => sum + agent.tools.length, 0) / agents.length
      : 0;
  const teamMembership = teams.reduce((sum, team) => sum + team.agents.length, 0);

  // Most connected room
  const roomConnections = rooms.map(room => ({
    room,
    connections: room.agents.length,
  }));
  const mostConnectedRoom = roomConnections.reduce(
    (max, curr) => (curr.connections > max.connections ? curr : max),
    roomConnections[0]
  );

  // Most active agent (most tools)
  const mostActiveAgent = agents.reduce(
    (max, curr) => (curr.tools.length > max.tools.length ? curr : max),
    agents[0]
  );

  return (
    <div className="w-full h-full overflow-hidden">
      <div className="grid grid-cols-3 gap-4 h-full">
        {/* Left: System Stats */}
        <div className="space-y-4">
          <div className="text-center p-4 bg-amber-50 dark:bg-amber-900/30 rounded-lg">
            <div className="flex justify-center mb-2">
              <div className="p-2 rounded-lg bg-amber-100 dark:bg-amber-900/30">
                <Bot className="w-5 h-5 text-amber-700 dark:text-amber-300" />
              </div>
            </div>
            <div className="text-2xl font-bold text-amber-900 dark:text-amber-100">
              {agents.length}
            </div>
            <div className="text-sm text-amber-700 dark:text-amber-300">Agents</div>
          </div>

          <div className="text-center p-4 bg-amber-50 dark:bg-amber-900/30 rounded-lg">
            <div className="flex justify-center mb-2">
              <div className="p-2 rounded-lg bg-amber-100 dark:bg-amber-900/30">
                <Home className="w-5 h-5 text-amber-700 dark:text-amber-300" />
              </div>
            </div>
            <div className="text-2xl font-bold text-amber-900 dark:text-amber-100">
              {rooms.length}
            </div>
            <div className="text-sm text-amber-700 dark:text-amber-300">Rooms</div>
          </div>

          <div className="text-center p-4 bg-amber-50 dark:bg-amber-900/30 rounded-lg">
            <div className="flex justify-center mb-2">
              <div className="p-2 rounded-lg bg-amber-100 dark:bg-amber-900/30">
                <Users className="w-5 h-5 text-amber-700 dark:text-amber-300" />
              </div>
            </div>
            <div className="text-2xl font-bold text-amber-900 dark:text-amber-100">
              {teams.length}
            </div>
            <div className="text-sm text-amber-700 dark:text-amber-300">Teams</div>
          </div>
        </div>

        {/* Center: Key Insights */}
        <div className="space-y-4">
          <div className="p-4 bg-amber-50 dark:bg-amber-900/30 rounded-lg">
            <div className="text-center mb-3">
              <div className="flex justify-center mb-2">
                <div className="p-2 rounded-lg bg-amber-100 dark:bg-amber-900/30">
                  <Link className="w-5 h-5 text-amber-700 dark:text-amber-300" />
                </div>
              </div>
              <div className="text-2xl font-bold text-amber-900 dark:text-amber-100">
                {totalConnections}
              </div>
              <div className="text-sm text-amber-700 dark:text-amber-300">Total Connections</div>
            </div>
          </div>

          {mostConnectedRoom && (
            <div
              className={`p-4 rounded-lg cursor-pointer transition-all hover:shadow-md ${getSelectionStyles(
                selectedRoomId === mostConnectedRoom.room.id,
                'card'
              )} ${
                selectedRoomId !== mostConnectedRoom.room.id
                  ? 'bg-amber-50 dark:bg-amber-900/30'
                  : ''
              }`}
              onClick={() => onSelectRoom(mostConnectedRoom.room.id)}
            >
              <div className="text-center">
                <div className="text-sm mb-2 flex items-center justify-center gap-1 text-amber-700 dark:text-amber-300">
                  <Trophy className="w-4 h-4" /> Most Connected Room
                </div>
                <div className="font-semibold text-amber-900 dark:text-amber-100">
                  {mostConnectedRoom.room.display_name}
                </div>
                <div className="text-sm text-amber-700 dark:text-amber-300">
                  {mostConnectedRoom.connections} agents
                </div>
              </div>
            </div>
          )}

          {mostActiveAgent && (
            <div
              className={`p-4 rounded-lg cursor-pointer transition-all hover:shadow-md ${getSelectionStyles(
                selectedAgentId === mostActiveAgent.id,
                'card'
              )} ${
                selectedAgentId !== mostActiveAgent.id ? 'bg-amber-50 dark:bg-amber-900/30' : ''
              }`}
              onClick={() => onSelectAgent(mostActiveAgent.id)}
            >
              <div className="text-center">
                <div className="text-sm mb-2 flex items-center justify-center gap-1 text-amber-700 dark:text-amber-300">
                  <Zap className="w-4 h-4" /> Most Active Agent
                </div>
                <div className="font-semibold text-amber-900 dark:text-amber-100">
                  {mostActiveAgent.display_name}
                </div>
                <div className="text-sm text-amber-700 dark:text-amber-300">
                  {mostActiveAgent.tools.length} tools
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Right: Relationships */}
        <div className="space-y-4">
          <div className="p-4 bg-amber-50 dark:bg-amber-900/30 rounded-lg">
            <h4 className="font-semibold mb-3 text-center flex items-center justify-center gap-1 text-amber-900 dark:text-amber-100">
              <BarChart3 className="w-4 h-4 text-amber-700 dark:text-amber-300" /> System Metrics
            </h4>
            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-amber-700 dark:text-amber-300">Avg. Tools/Agent:</span>
                <span className="font-semibold text-amber-900 dark:text-amber-100">
                  {averageToolsPerAgent.toFixed(1)}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-amber-700 dark:text-amber-300">Team Members:</span>
                <span className="font-semibold text-amber-900 dark:text-amber-100">
                  {teamMembership}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-amber-700 dark:text-amber-300">Avg. Agents/Room:</span>
                <span className="font-semibold text-amber-900 dark:text-amber-100">
                  {rooms.length > 0 ? (totalConnections / rooms.length).toFixed(1) : '0'}
                </span>
              </div>
            </div>
          </div>

          <div className="p-4 bg-amber-50 dark:bg-amber-900/30 rounded-lg">
            <h4 className="font-semibold mb-3 text-center text-amber-900 dark:text-amber-100 flex items-center justify-center gap-1">
              <Activity className="w-4 h-4 text-amber-700 dark:text-amber-300" /> Quick Actions
            </h4>
            <div className="space-y-2 text-sm">
              <button
                className="w-full p-2 text-left rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors text-amber-700 dark:text-amber-300 flex items-center"
                onClick={() => {
                  onSelectAgent(null);
                  onSelectRoom(null);
                }}
              >
                <X className="w-4 h-4 mr-2" /> Clear Selection
              </button>
              <div className="text-xs text-amber-600 dark:text-amber-400 text-center mt-3">
                Click items above to explore relationships
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
