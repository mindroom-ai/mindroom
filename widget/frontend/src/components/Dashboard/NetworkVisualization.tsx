import { useState, useMemo } from 'react';
import { Agent, Room, Team } from '@/types/config';

interface NetworkNode {
  id: string;
  name: string;
  type: 'agent' | 'room' | 'team';
  x: number;
  y: number;
  data: Agent | Room | Team;
}

interface NetworkLink {
  source: string;
  target: string;
  type: string;
}

interface NetworkVisualizationProps {
  agents: Agent[];
  rooms: Room[];
  teams: Team[];
  selectedAgentId: string | null;
  selectedRoomId: string | null;
  onSelectAgent: (agentId: string | null) => void;
  onSelectRoom: (roomId: string | null) => void;
}

export function NetworkVisualization({
  agents,
  rooms,
  teams,
  selectedAgentId,
  selectedRoomId,
  onSelectAgent,
  onSelectRoom,
}: NetworkVisualizationProps) {
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  // Calculate positions and create network data
  const { nodes, links } = useMemo(() => {
    const width = 1000;
    const height = 600;
    const nodes: NetworkNode[] = [];
    const links: NetworkLink[] = [];

    // Position rooms in the center area
    const roomCenterX = width / 2;
    const roomCenterY = height / 2;
    const roomRadius = Math.min(120, (rooms.length * 40) / (2 * Math.PI));

    rooms.forEach((room, index) => {
      const angle = (index / rooms.length) * 2 * Math.PI;
      nodes.push({
        id: `room-${room.id}`,
        name: room.display_name,
        type: 'room',
        x: roomCenterX + Math.cos(angle) * roomRadius,
        y: roomCenterY + Math.sin(angle) * roomRadius,
        data: room,
      });
    });

    // Position agents around rooms
    agents.forEach((agent, index) => {
      // Find the room this agent is most connected to, or use a default position
      let targetX = 150;
      let targetY = 100 + (index % 8) * 60;

      if (agent.rooms.length > 0) {
        const primaryRoom = agent.rooms[0];
        const roomNode = nodes.find(n => n.id === `room-${primaryRoom}`);
        if (roomNode) {
          // Position agents around their primary room
          const agentAngle = (agents.indexOf(agent) * 60) % 360;
          const distance = 150;
          targetX = roomNode.x + Math.cos((agentAngle * Math.PI) / 180) * distance;
          targetY = roomNode.y + Math.sin((agentAngle * Math.PI) / 180) * distance;
        }
      }

      nodes.push({
        id: `agent-${agent.id}`,
        name: agent.display_name,
        type: 'agent',
        x: Math.max(50, Math.min(width - 50, targetX)),
        y: Math.max(50, Math.min(height - 50, targetY)),
        data: agent,
      });
    });

    // Position teams on the right side
    teams.forEach((team, index) => {
      nodes.push({
        id: `team-${team.id}`,
        name: team.display_name,
        type: 'team',
        x: width - 150,
        y: 100 + index * 120,
        data: team,
      });
    });

    // Create links
    // Agent-Room links
    agents.forEach(agent => {
      agent.rooms.forEach(roomId => {
        const roomExists = nodes.find(n => n.id === `room-${roomId}`);
        if (roomExists) {
          links.push({
            source: `agent-${agent.id}`,
            target: `room-${roomId}`,
            type: 'agent-room',
          });
        }
      });
    });

    // Team-Agent links
    teams.forEach(team => {
      team.agents.forEach(agentId => {
        const agentExists = nodes.find(n => n.id === `agent-${agentId}`);
        if (agentExists) {
          links.push({
            source: `team-${team.id}`,
            target: `agent-${agentId}`,
            type: 'team-agent',
          });
        }
      });
    });

    // Team-Room links
    teams.forEach(team => {
      team.rooms.forEach(roomId => {
        const roomExists = nodes.find(n => n.id === `room-${roomId}`);
        if (roomExists) {
          links.push({
            source: `team-${team.id}`,
            target: `room-${roomId}`,
            type: 'team-room',
          });
        }
      });
    });

    return { nodes, links };
  }, [agents, rooms, teams]);

  const getNodeColor = (node: NetworkNode) => {
    if (node.type === 'agent') {
      return selectedAgentId === (node.data as Agent).id ? '#f59e0b' : '#3b82f6';
    } else if (node.type === 'room') {
      return selectedRoomId === (node.data as Room).id ? '#f59e0b' : '#10b981';
    } else {
      return '#8b5cf6';
    }
  };

  const getNodeSize = (node: NetworkNode) => {
    if (node.type === 'room') {
      return 15 + (node.data as Room).agents.length * 2;
    } else if (node.type === 'agent') {
      return 10 + (node.data as Agent).tools.length * 0.5;
    } else {
      return 12 + (node.data as Team).agents.length * 1.5;
    }
  };

  const getNodeIcon = (type: string) => {
    switch (type) {
      case 'agent':
        return '🤖';
      case 'room':
        return '🏠';
      case 'team':
        return '👥';
      default:
        return '';
    }
  };

  const handleNodeClick = (node: NetworkNode) => {
    console.log('Node clicked:', node.type, node.name); // Debug log
    if (node.type === 'agent') {
      onSelectAgent((node.data as Agent).id);
    } else if (node.type === 'room') {
      onSelectRoom((node.data as Room).id);
    }
  };

  const getLinkColor = (type: string) => {
    switch (type) {
      case 'agent-room':
        return '#94a3b8';
      case 'team-agent':
        return '#c084fc';
      case 'team-room':
        return '#a855f7';
      default:
        return '#9ca3af';
    }
  };

  const getLinkWidth = (type: string) => {
    switch (type) {
      case 'team-room':
        return 3;
      case 'team-agent':
        return 2;
      default:
        return 1.5;
    }
  };

  return (
    <div className="w-full bg-gray-50 dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
      <svg
        width="100%"
        height="600"
        viewBox="0 0 1000 600"
        className="overflow-visible"
        style={{ userSelect: 'none' }}
        onClick={e => {
          console.log('SVG clicked', e);
        }}
      >
        {/* Links */}
        {links.map((link, index) => {
          const sourceNode = nodes.find(n => n.id === link.source);
          const targetNode = nodes.find(n => n.id === link.target);
          if (!sourceNode || !targetNode) return null;

          return (
            <line
              key={index}
              x1={sourceNode.x}
              y1={sourceNode.y}
              x2={targetNode.x}
              y2={targetNode.y}
              stroke={getLinkColor(link.type)}
              strokeWidth={getLinkWidth(link.type)}
              strokeOpacity={0.6}
              className="transition-all duration-200"
            />
          );
        })}

        {/* Nodes */}
        {nodes.map(node => {
          const size = getNodeSize(node);
          const isSelected =
            (node.type === 'agent' && selectedAgentId === (node.data as Agent).id) ||
            (node.type === 'room' && selectedRoomId === (node.data as Room).id);
          const isHovered = hoveredNode === node.id;

          return (
            <g
              key={node.id}
              className="cursor-pointer"
              onClick={e => {
                e.stopPropagation();
                handleNodeClick(node);
              }}
              onMouseEnter={() => setHoveredNode(node.id)}
              onMouseLeave={() => setHoveredNode(null)}
            >
              {/* Node circle */}
              <circle
                cx={node.x}
                cy={node.y}
                r={size + (isHovered ? 3 : 0)}
                fill={getNodeColor(node)}
                stroke={isSelected ? '#f59e0b' : '#fff'}
                strokeWidth={isSelected ? 3 : 2}
                className="transition-all duration-200 hover:drop-shadow-lg"
              />

              {/* Node icon */}
              <text
                x={node.x}
                y={node.y}
                textAnchor="middle"
                dominantBaseline="central"
                fontSize={Math.max(10, size * 0.8)}
                className="pointer-events-none select-none"
              >
                {getNodeIcon(node.type)}
              </text>

              {/* Node label */}
              <text
                x={node.x}
                y={node.y + size + 20}
                textAnchor="middle"
                fontSize="12"
                fontWeight="bold"
                fill="currentColor"
                className="pointer-events-none select-none"
              >
                {node.name.length > 10 ? node.name.substring(0, 10) + '...' : node.name}
              </text>

              {/* Hover tooltip */}
              {isHovered && (
                <g>
                  <rect
                    x={node.x + 20}
                    y={node.y - 30}
                    width="140"
                    height="60"
                    fill="rgba(0, 0, 0, 0.8)"
                    rx="4"
                    className="pointer-events-none"
                  />
                  <text
                    x={node.x + 25}
                    y={node.y - 15}
                    fontSize="11"
                    fill="white"
                    className="pointer-events-none"
                  >
                    {getNodeIcon(node.type)} {node.name}
                  </text>
                  <text
                    x={node.x + 25}
                    y={node.y}
                    fontSize="10"
                    fill="white"
                    className="pointer-events-none"
                  >
                    {node.type === 'agent' &&
                      `Tools: ${(node.data as Agent).tools.length}, Rooms: ${
                        (node.data as Agent).rooms.length
                      }`}
                    {node.type === 'room' && `Agents: ${(node.data as Room).agents.length}`}
                    {node.type === 'team' &&
                      `Members: ${(node.data as Team).agents.length}, Mode: ${
                        (node.data as Team).mode
                      }`}
                  </text>
                </g>
              )}
            </g>
          );
        })}
      </svg>

      {/* Legend */}
      <div className="mt-4 flex justify-center space-x-8 text-sm text-gray-600 dark:text-gray-400">
        <div className="flex items-center space-x-2">
          <div className="w-4 h-4 bg-blue-500 rounded-full"></div>
          <span>🤖 Agents</span>
        </div>
        <div className="flex items-center space-x-2">
          <div className="w-4 h-4 bg-green-500 rounded-full"></div>
          <span>🏠 Rooms</span>
        </div>
        <div className="flex items-center space-x-2">
          <div className="w-4 h-4 bg-purple-500 rounded-full"></div>
          <span>👥 Teams</span>
        </div>
        <div className="text-xs">
          💡 Node size = activity level • Click to select • Hover for details
        </div>
      </div>
    </div>
  );
}
