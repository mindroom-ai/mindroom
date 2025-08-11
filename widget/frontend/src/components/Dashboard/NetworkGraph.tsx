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
  return (
    <div className="w-full h-full bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden flex items-center justify-center">
      <div className="text-center p-8">
        <h3 className="text-xl font-semibold mb-4">Network Visualization</h3>
        <div className="grid grid-cols-3 gap-8 mb-6">
          <div className="text-center">
            <div className="text-4xl mb-2">🤖</div>
            <div className="text-lg font-semibold">{agents.length}</div>
            <div className="text-sm text-gray-600 dark:text-gray-400">Agents</div>
          </div>
          <div className="text-center">
            <div className="text-4xl mb-2">🏠</div>
            <div className="text-lg font-semibold">{rooms.length}</div>
            <div className="text-sm text-gray-600 dark:text-gray-400">Rooms</div>
          </div>
          <div className="text-center">
            <div className="text-4xl mb-2">👥</div>
            <div className="text-lg font-semibold">{teams.length}</div>
            <div className="text-sm text-gray-600 dark:text-gray-400">Teams</div>
          </div>
        </div>
        <p className="text-gray-600 dark:text-gray-400 max-w-md mx-auto">
          Interactive network graph temporarily disabled due to library conflicts. The relationships
          are visualized in the lists below.
        </p>
      </div>
    </div>
  );
}
