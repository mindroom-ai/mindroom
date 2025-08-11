import { useRef, useEffect, useState } from 'react';
import { ForceGraph2D } from 'react-force-graph';
import { Agent, Room, Team } from '@/types/config';

interface NetworkNode {
  id: string;
  name: string;
  type: 'agent' | 'room' | 'team';
  color: string;
  size: number;
  data: Agent | Room | Team;
  x?: number;
  y?: number;
}

interface NetworkLink {
  source: string;
  target: string;
  type: 'agent-room' | 'agent-team' | 'team-room';
  color: string;
  width: number;
}

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

export function NetworkGraph({
  agents,
  rooms,
  teams,
  selectedAgentId,
  selectedRoomId,
  onSelectAgent,
  onSelectRoom,
  width = 800,
  height = 600,
}: NetworkGraphProps) {
  const fgRef = useRef<any>();
  const [graphData, setGraphData] = useState<{ nodes: NetworkNode[]; links: NetworkLink[] }>({
    nodes: [],
    links: [],
  });

  // Generate graph data from agents, rooms, and teams
  useEffect(() => {
    const nodes: NetworkNode[] = [];
    const links: NetworkLink[] = [];

    // Color schemes
    const colors = {
      agent: '#3B82F6', // Blue
      room: '#10B981', // Green
      team: '#8B5CF6', // Purple
      selected: '#F59E0B', // Orange
    };

    // Add room nodes (larger, central hubs)
    rooms.forEach(room => {
      nodes.push({
        id: `room-${room.id}`,
        name: room.display_name,
        type: 'room',
        color: selectedRoomId === room.id ? colors.selected : colors.room,
        size: 12 + room.agents.length * 2, // Size based on agent count
        data: room,
      });
    });

    // Add agent nodes
    agents.forEach(agent => {
      nodes.push({
        id: `agent-${agent.id}`,
        name: agent.display_name,
        type: 'agent',
        color: selectedAgentId === agent.id ? colors.selected : colors.agent,
        size: 8 + agent.tools.length * 0.5, // Size based on tool count
        data: agent,
      });

      // Add links from agents to their rooms
      agent.rooms.forEach(roomId => {
        links.push({
          source: `agent-${agent.id}`,
          target: `room-${roomId}`,
          type: 'agent-room',
          color: '#94A3B8', // Gray
          width: 2,
        });
      });
    });

    // Add team nodes
    teams.forEach(team => {
      nodes.push({
        id: `team-${team.id}`,
        name: team.display_name,
        type: 'team',
        color: colors.team,
        size: 10 + team.agents.length, // Size based on member count
        data: team,
      });

      // Add links from team to its agents
      team.agents.forEach(agentId => {
        links.push({
          source: `team-${team.id}`,
          target: `agent-${agentId}`,
          type: 'agent-team',
          color: '#C084FC', // Light purple
          width: 1.5,
        });
      });

      // Add links from team to its rooms
      team.rooms.forEach(roomId => {
        links.push({
          source: `team-${team.id}`,
          target: `room-${roomId}`,
          type: 'team-room',
          color: '#A855F7', // Purple
          width: 3,
        });
      });
    });

    setGraphData({ nodes, links });
  }, [agents, rooms, teams, selectedAgentId, selectedRoomId]);

  // Handle node clicks
  const handleNodeClick = (node: NetworkNode) => {
    if (node.type === 'agent') {
      const agentId = node.id.replace('agent-', '');
      onSelectAgent(agentId);
    } else if (node.type === 'room') {
      const roomId = node.id.replace('room-', '');
      onSelectRoom(roomId);
    }
  };

  return (
    <div className="w-full h-full bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      <ForceGraph2D
        ref={fgRef}
        graphData={graphData}
        width={width}
        height={height}
        nodeLabel={(node: any) => {
          const n = node as NetworkNode;
          return `<div style="
            background: rgba(0,0,0,0.8);
            color: white;
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 12px;
            max-width: 200px;
          ">
            <strong>${n.name}</strong><br/>
            Type: ${n.type}<br/>
            ${
              n.type === 'agent'
                ? `Tools: ${(n.data as Agent).tools.length}<br/>Rooms: ${
                    (n.data as Agent).rooms.length
                  }`
                : n.type === 'room'
                  ? `Agents: ${(n.data as Room).agents.length}`
                  : `Members: ${(n.data as Team).agents.length}<br/>Mode: ${(n.data as Team).mode}`
            }
          </div>`;
        }}
        nodeColor={(node: any) => (node as NetworkNode).color}
        nodeVal={(node: any) => (node as NetworkNode).size}
        linkColor={(link: any) => (link as NetworkLink).color}
        linkWidth={(link: any) => (link as NetworkLink).width}
        linkDirectionalParticles={2}
        linkDirectionalParticleWidth={2}
        onNodeClick={handleNodeClick}
        onNodeHover={(node: any) => {
          if (node) {
            document.body.style.cursor = 'pointer';
          } else {
            document.body.style.cursor = 'default';
          }
        }}
        cooldownTicks={100}
        d3AlphaMin={0.02}
        d3VelocityDecay={0.3}
        nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
          const n = node as NetworkNode;
          const label = n.name;
          const fontSize = 12 / globalScale;

          // Draw node circle
          ctx.fillStyle = n.color;
          ctx.beginPath();
          ctx.arc(node.x!, node.y!, n.size / 2, 0, 2 * Math.PI, false);
          ctx.fill();

          // Add emoji icons
          const emoji = n.type === 'agent' ? '🤖' : n.type === 'room' ? '🏠' : '👥';
          ctx.font = `${fontSize * 1.2}px Sans-Serif`;
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillText(emoji, node.x!, node.y!);

          // Draw label if zoomed in enough
          if (globalScale > 1) {
            ctx.font = `${fontSize}px Sans-Serif`;
            ctx.fillStyle = 'rgba(0,0,0,0.8)';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(label, node.x!, node.y! + n.size / 2 + 2);
          }
        }}
        backgroundColor="transparent"
      />
    </div>
  );
}
