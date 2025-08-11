import { useEffect, useRef, useState } from 'react';
import * as d3 from 'd3';
import { Agent, Room, Team } from '@/types/config';

interface NetworkNode {
  id: string;
  name: string;
  type: 'agent' | 'room' | 'team';
  size: number;
  color: string;
  data: Agent | Room | Team;
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
}

interface NetworkLink {
  source: string | NetworkNode;
  target: string | NetworkNode;
  type: string;
  distance: number;
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
  width = 1000,
  height = 400,
}: NetworkGraphProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; content: string } | null>(null);

  useEffect(() => {
    if (!svgRef.current) return;

    // Clear previous content
    d3.select(svgRef.current).selectAll('*').remove();

    // Create nodes
    const nodes: NetworkNode[] = [];

    // Add room nodes (larger, central hubs)
    rooms.forEach(room => {
      nodes.push({
        id: `room-${room.id}`,
        name: room.display_name,
        type: 'room',
        size: 20 + room.agents.length * 3,
        color: selectedRoomId === room.id ? '#f59e0b' : '#10b981',
        data: room,
      });
    });

    // Add agent nodes
    agents.forEach(agent => {
      nodes.push({
        id: `agent-${agent.id}`,
        name: agent.display_name,
        type: 'agent',
        size: 12 + agent.tools.length * 1.5,
        color: selectedAgentId === agent.id ? '#f59e0b' : '#3b82f6',
        data: agent,
      });
    });

    // Add team nodes
    teams.forEach(team => {
      nodes.push({
        id: `team-${team.id}`,
        name: team.display_name,
        type: 'team',
        size: 15 + team.agents.length * 2,
        color: '#8b5cf6',
        data: team,
      });
    });

    // Create links
    const links: NetworkLink[] = [];

    // Create a set of existing node IDs for validation
    const nodeIds = new Set(nodes.map(n => n.id));

    // Agent-Room links
    agents.forEach(agent => {
      agent.rooms.forEach(roomId => {
        const targetId = `room-${roomId}`;
        if (nodeIds.has(targetId)) {
          links.push({
            source: `agent-${agent.id}`,
            target: targetId,
            type: 'agent-room',
            distance: 80,
          });
        }
      });
    });

    // Team-Agent links
    teams.forEach(team => {
      team.agents.forEach(agentId => {
        const targetId = `agent-${agentId}`;
        if (nodeIds.has(targetId)) {
          links.push({
            source: `team-${team.id}`,
            target: targetId,
            type: 'team-agent',
            distance: 60,
          });
        }
      });
    });

    // Team-Room links
    teams.forEach(team => {
      team.rooms.forEach(roomId => {
        const targetId = `room-${roomId}`;
        if (nodeIds.has(targetId)) {
          links.push({
            source: `team-${team.id}`,
            target: targetId,
            type: 'team-room',
            distance: 100,
          });
        }
      });
    });

    // Create SVG
    const svg = d3.select(svgRef.current).attr('width', width).attr('height', height);

    // Create force simulation
    const simulation = d3
      .forceSimulation<NetworkNode>(nodes)
      .force(
        'link',
        d3
          .forceLink<NetworkNode, NetworkLink>(links)
          .id(d => d.id)
          .distance(d => d.distance)
          .strength(0.3)
      )
      .force('charge', d3.forceManyBody().strength(-400))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force(
        'collision',
        d3.forceCollide().radius(d => (d as NetworkNode).size + 5)
      );

    // Create links
    const linkElements = svg
      .append('g')
      .selectAll('line')
      .data(links)
      .join('line')
      .attr('stroke', d => {
        switch (d.type) {
          case 'agent-room':
            return '#94a3b8';
          case 'team-agent':
            return '#c084fc';
          case 'team-room':
            return '#a855f7';
          default:
            return '#9ca3af';
        }
      })
      .attr('stroke-width', d => {
        switch (d.type) {
          case 'team-room':
            return 3;
          case 'team-agent':
            return 2;
          default:
            return 1.5;
        }
      })
      .attr('stroke-opacity', 0.6);

    // Create node groups
    const nodeGroups = svg
      .append('g')
      .selectAll('g')
      .data(nodes)
      .join('g')
      .style('cursor', 'pointer')
      .on('click', (event, d) => {
        event.stopPropagation();
        if (d.type === 'agent') {
          const agent = d.data as Agent;
          onSelectAgent(agent.id);
        } else if (d.type === 'room') {
          const room = d.data as Room;
          onSelectRoom(room.id);
        }
      })
      .on('mouseover', (event, d) => {
        const rect = svgRef.current!.getBoundingClientRect();
        let tooltipContent = '';

        if (d.type === 'agent') {
          const agent = d.data as Agent;
          tooltipContent = `🤖 ${d.name}<br/>Tools: ${agent.tools.length}<br/>Rooms: ${agent.rooms.length}`;
        } else if (d.type === 'room') {
          const room = d.data as Room;
          tooltipContent = `🏠 ${d.name}<br/>Agents: ${room.agents.length}`;
        } else if (d.type === 'team') {
          const team = d.data as Team;
          tooltipContent = `👥 ${d.name}<br/>Members: ${team.agents.length}<br/>Mode: ${team.mode}`;
        }

        setTooltip({
          x: event.clientX - rect.left + 10,
          y: event.clientY - rect.top - 10,
          content: tooltipContent,
        });
      })
      .on('mouseout', () => {
        setTooltip(null);
      });

    // Add circles to node groups
    nodeGroups
      .append('circle')
      .attr('r', d => d.size)
      .attr('fill', d => d.color)
      .attr('stroke', '#fff')
      .attr('stroke-width', 2);

    // Add emoji icons
    nodeGroups
      .append('text')
      .attr('text-anchor', 'middle')
      .attr('dy', '0.35em')
      .attr('font-size', d => Math.max(10, d.size * 0.6))
      .text(d => {
        switch (d.type) {
          case 'agent':
            return '🤖';
          case 'room':
            return '🏠';
          case 'team':
            return '👥';
          default:
            return '';
        }
      });

    // Add labels
    nodeGroups
      .append('text')
      .attr('text-anchor', 'middle')
      .attr('dy', d => d.size + 15)
      .attr('font-size', '11px')
      .attr('font-weight', 'bold')
      .attr('fill', 'currentColor')
      .text(d => (d.name.length > 12 ? d.name.substring(0, 12) + '...' : d.name));

    // Add drag behavior
    const drag = d3
      .drag<SVGGElement, NetworkNode>()
      .on('start', (event, d) => {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on('drag', (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
      })
      .on('end', (event, d) => {
        if (!event.active) simulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
      });

    nodeGroups.call(drag as any);

    // Update positions on simulation tick
    simulation.on('tick', () => {
      linkElements
        .attr('x1', d => (d.source as NetworkNode).x!)
        .attr('y1', d => (d.source as NetworkNode).y!)
        .attr('x2', d => (d.target as NetworkNode).x!)
        .attr('y2', d => (d.target as NetworkNode).y!);

      nodeGroups.attr('transform', d => `translate(${d.x},${d.y})`);
    });

    // Cleanup
    return () => {
      simulation.stop();
    };
  }, [
    agents,
    rooms,
    teams,
    selectedAgentId,
    selectedRoomId,
    onSelectAgent,
    onSelectRoom,
    width,
    height,
  ]);

  return (
    <div className="relative w-full h-full bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      <svg ref={svgRef} className="w-full h-full" />
      {tooltip && (
        <div
          className="absolute z-10 bg-black text-white text-xs rounded px-2 py-1 pointer-events-none"
          style={{ left: tooltip.x, top: tooltip.y }}
          dangerouslySetInnerHTML={{ __html: tooltip.content }}
        />
      )}
    </div>
  );
}
