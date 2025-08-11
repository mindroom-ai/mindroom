import { Agent, Room, Team } from '@/types/config';

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
          <div className="text-center p-4 bg-blue-50 dark:bg-blue-950 rounded-lg">
            <div className="text-2xl mb-2">🤖</div>
            <div className="text-2xl font-bold text-blue-700 dark:text-blue-300">
              {agents.length}
            </div>
            <div className="text-sm text-blue-600 dark:text-blue-400">Agents</div>
          </div>

          <div className="text-center p-4 bg-green-50 dark:bg-green-950 rounded-lg">
            <div className="text-2xl mb-2">🏠</div>
            <div className="text-2xl font-bold text-green-700 dark:text-green-300">
              {rooms.length}
            </div>
            <div className="text-sm text-green-600 dark:text-green-400">Rooms</div>
          </div>

          <div className="text-center p-4 bg-purple-50 dark:bg-purple-950 rounded-lg">
            <div className="text-2xl mb-2">👥</div>
            <div className="text-2xl font-bold text-purple-700 dark:text-purple-300">
              {teams.length}
            </div>
            <div className="text-sm text-purple-600 dark:text-purple-400">Teams</div>
          </div>
        </div>

        {/* Center: Key Insights */}
        <div className="space-y-4">
          <div className="p-4 bg-orange-50 dark:bg-orange-950 rounded-lg">
            <div className="text-center mb-3">
              <div className="text-2xl mb-2">🔗</div>
              <div className="text-2xl font-bold text-orange-700 dark:text-orange-300">
                {totalConnections}
              </div>
              <div className="text-sm text-orange-600 dark:text-orange-400">Total Connections</div>
            </div>
          </div>

          {mostConnectedRoom && (
            <div
              className={`p-4 rounded-lg cursor-pointer transition-all hover:shadow-md ${
                selectedRoomId === mostConnectedRoom.room.id
                  ? 'ring-2 ring-green-500 bg-green-100 dark:bg-green-900'
                  : 'bg-green-50 dark:bg-green-950 hover:bg-green-100 dark:hover:bg-green-900'
              }`}
              onClick={() => onSelectRoom(mostConnectedRoom.room.id)}
            >
              <div className="text-center">
                <div className="text-lg mb-1">🏆 Most Connected Room</div>
                <div className="font-semibold text-green-700 dark:text-green-300">
                  {mostConnectedRoom.room.display_name}
                </div>
                <div className="text-sm text-green-600 dark:text-green-400">
                  {mostConnectedRoom.connections} agents
                </div>
              </div>
            </div>
          )}

          {mostActiveAgent && (
            <div
              className={`p-4 rounded-lg cursor-pointer transition-all hover:shadow-md ${
                selectedAgentId === mostActiveAgent.id
                  ? 'ring-2 ring-blue-500 bg-blue-100 dark:bg-blue-900'
                  : 'bg-blue-50 dark:bg-blue-950 hover:bg-blue-100 dark:hover:bg-blue-900'
              }`}
              onClick={() => onSelectAgent(mostActiveAgent.id)}
            >
              <div className="text-center">
                <div className="text-lg mb-1">⚡ Most Active Agent</div>
                <div className="font-semibold text-blue-700 dark:text-blue-300">
                  {mostActiveAgent.display_name}
                </div>
                <div className="text-sm text-blue-600 dark:text-blue-400">
                  {mostActiveAgent.tools.length} tools
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Right: Relationships */}
        <div className="space-y-4">
          <div className="p-4 bg-gray-50 dark:bg-gray-900 rounded-lg">
            <h4 className="font-semibold mb-3 text-center">📊 System Metrics</h4>
            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-600 dark:text-gray-400">Avg. Tools/Agent:</span>
                <span className="font-semibold">{averageToolsPerAgent.toFixed(1)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600 dark:text-gray-400">Team Members:</span>
                <span className="font-semibold">{teamMembership}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-600 dark:text-gray-400">Avg. Agents/Room:</span>
                <span className="font-semibold">
                  {rooms.length > 0 ? (totalConnections / rooms.length).toFixed(1) : '0'}
                </span>
              </div>
            </div>
          </div>

          <div className="p-4 bg-purple-50 dark:bg-purple-950 rounded-lg">
            <h4 className="font-semibold mb-3 text-center text-purple-700 dark:text-purple-300">
              🎯 Quick Actions
            </h4>
            <div className="space-y-2 text-sm">
              <button
                className="w-full p-2 text-left rounded hover:bg-purple-100 dark:hover:bg-purple-900 transition-colors"
                onClick={() => {
                  onSelectAgent(null);
                  onSelectRoom(null);
                }}
              >
                📋 Clear Selection
              </button>
              <div className="text-xs text-purple-600 dark:text-purple-400 text-center mt-3">
                Click items above to explore relationships
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
