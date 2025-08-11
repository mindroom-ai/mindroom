import { useState, useMemo, useEffect, useCallback } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { NetworkGraph } from './NetworkGraph';

export function Dashboard() {
  const { agents, rooms, teams, config, selectedRoomId, selectedAgentId, selectRoom, selectAgent } =
    useConfigStore();

  // Search and filter state
  const [searchTerm, setSearchTerm] = useState('');
  const [showTypes, setShowTypes] = useState<string[]>(['agents', 'rooms', 'teams']);

  // Real-time status simulation (replace with actual WebSocket connection)
  const [lastUpdated, setLastUpdated] = useState(new Date());

  // Memoized status functions for performance
  const getAgentStatus = useCallback((agentId: string) => {
    const hash = agentId.split('').reduce((a, b) => a + b.charCodeAt(0), 0);
    const statusOptions = ['online', 'busy', 'idle', 'offline'] as const;
    return statusOptions[hash % statusOptions.length];
  }, []);

  const getStatusColor = useCallback((status: string) => {
    switch (status) {
      case 'online':
        return 'bg-green-500';
      case 'busy':
        return 'bg-orange-500';
      case 'idle':
        return 'bg-yellow-500';
      case 'offline':
        return 'bg-gray-400';
      default:
        return 'bg-gray-400';
    }
  }, []);

  const getStatusLabel = useCallback((status: string) => {
    switch (status) {
      case 'online':
        return 'Online';
      case 'busy':
        return 'Busy';
      case 'idle':
        return 'Idle';
      case 'offline':
        return 'Offline';
      default:
        return 'Unknown';
    }
  }, []);

  // Simulate periodic updates
  useEffect(() => {
    const interval = setInterval(() => {
      setLastUpdated(new Date());
    }, 30000); // Update every 30 seconds

    return () => clearInterval(interval);
  }, []);

  // Calculate system stats with real-time status
  const stats = useMemo(() => {
    const agentStatuses = agents.map(agent => getAgentStatus(agent.id));
    return {
      totalAgents: agents.length,
      totalRooms: rooms.length,
      totalTeams: teams.length,
      modelsInUse: config ? Object.keys(config.models).length : 0,
      agentsOnline: agentStatuses.filter(status => status === 'online').length,
      agentsBusy: agentStatuses.filter(status => status === 'busy').length,
      agentsIdle: agentStatuses.filter(status => status === 'idle').length,
      agentsOffline: agentStatuses.filter(status => status === 'offline').length,
      activeConnections: rooms.length,
    };
  }, [agents, rooms, teams, config, lastUpdated]);

  // Filter data based on search and type filters
  const filteredData = useMemo(() => {
    const searchLower = searchTerm.toLowerCase();

    return {
      agents: showTypes.includes('agents')
        ? agents.filter(
            agent =>
              agent.display_name.toLowerCase().includes(searchLower) ||
              agent.role.toLowerCase().includes(searchLower) ||
              agent.tools.some(tool => tool.toLowerCase().includes(searchLower)) ||
              agent.rooms.some(room => room.toLowerCase().includes(searchLower))
          )
        : [],
      rooms: showTypes.includes('rooms')
        ? rooms.filter(
            room =>
              room.display_name.toLowerCase().includes(searchLower) ||
              room.id.toLowerCase().includes(searchLower)
          )
        : [],
      teams: showTypes.includes('teams')
        ? teams.filter(
            team =>
              team.display_name.toLowerCase().includes(searchLower) ||
              team.role.toLowerCase().includes(searchLower) ||
              team.mode.toLowerCase().includes(searchLower)
          )
        : [],
    };
  }, [agents, rooms, teams, searchTerm, showTypes]);

  // Get selected room details
  const selectedRoom = selectedRoomId ? rooms.find(r => r.id === selectedRoomId) : null;
  const selectedAgent = selectedAgentId ? agents.find(a => a.id === selectedAgentId) : null;

  // Memoized export configuration function
  const exportConfiguration = useCallback(() => {
    const exportData = {
      timestamp: new Date().toISOString(),
      stats,
      agents: agents.map(agent => ({
        ...agent,
        teamMemberships: teams
          .filter(team => team.agents.includes(agent.id))
          .map(team => team.display_name),
      })),
      rooms: rooms.map(room => ({
        ...room,
        teamsInRoom: teams
          .filter(team => team.rooms.includes(room.id))
          .map(team => team.display_name),
      })),
      teams,
      modelConfigurations: config?.models || {},
    };

    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `mindroom-config-${new Date().toISOString().split('T')[0]}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [agents, rooms, teams, config, stats]);

  return (
    <div className="flex flex-col h-full gap-4">
      {/* Header with Quick Actions */}
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold">System Overview</h2>
          <p className="text-gray-600 dark:text-gray-400">
            Monitor your MindRoom configuration and status
          </p>
          <p className="text-xs text-gray-500 mt-1">
            🔄 Last updated: {lastUpdated.toLocaleTimeString()}
          </p>
        </div>
        <div className="flex gap-2 items-center">
          <Input
            placeholder="Search agents, rooms, teams..."
            value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
            className="w-64"
          />
          <ToggleGroup type="multiple" value={showTypes} onValueChange={setShowTypes}>
            <ToggleGroupItem value="agents">🤖 Agents</ToggleGroupItem>
            <ToggleGroupItem value="rooms">🏠 Rooms</ToggleGroupItem>
            <ToggleGroupItem value="teams">👥 Teams</ToggleGroupItem>
          </ToggleGroup>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              selectAgent(null);
              selectRoom(null);
            }}
          >
            Clear Selection
          </Button>
          <Button variant="outline" size="sm" onClick={exportConfiguration}>
            📄 Export Config
          </Button>
        </div>
      </div>

      {/* System Stats Cards - Top Bar */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card className="bg-gradient-to-br from-blue-50 to-blue-100 dark:from-blue-950 dark:to-blue-900 border-blue-200 dark:border-blue-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-2xl font-bold text-blue-700 dark:text-blue-300">
              {stats.totalAgents}
            </CardTitle>
            <CardDescription className="text-blue-600 dark:text-blue-400">
              🤖 Agents
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-4 text-sm text-blue-600 dark:text-blue-400">
              <span>🟢 {stats.agentsOnline}</span>
              <span>🟠 {stats.agentsBusy}</span>
              <span>🟡 {stats.agentsIdle}</span>
              <span>⚫ {stats.agentsOffline}</span>
            </div>
          </CardContent>
        </Card>

        <Card className="bg-gradient-to-br from-green-50 to-green-100 dark:from-green-950 dark:to-green-900 border-green-200 dark:border-green-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-2xl font-bold text-green-700 dark:text-green-300">
              {stats.totalRooms}
            </CardTitle>
            <CardDescription className="text-green-600 dark:text-green-400">
              🏠 Rooms
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-green-600 dark:text-green-400">
              {stats.activeConnections} configured
            </p>
          </CardContent>
        </Card>

        <Card className="bg-gradient-to-br from-purple-50 to-purple-100 dark:from-purple-950 dark:to-purple-900 border-purple-200 dark:border-purple-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-2xl font-bold text-purple-700 dark:text-purple-300">
              {stats.totalTeams}
            </CardTitle>
            <CardDescription className="text-purple-600 dark:text-purple-400">
              👥 Teams
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-purple-600 dark:text-purple-400">
              {teams.reduce((acc, team) => acc + team.agents.length, 0)} members
            </p>
          </CardContent>
        </Card>

        <Card className="bg-gradient-to-br from-orange-50 to-orange-100 dark:from-orange-950 dark:to-orange-900 border-orange-200 dark:border-orange-800">
          <CardHeader className="pb-2">
            <CardTitle className="text-2xl font-bold text-orange-700 dark:text-orange-300">
              {stats.modelsInUse}
            </CardTitle>
            <CardDescription className="text-orange-600 dark:text-orange-400">
              🔧 Models
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-orange-600 dark:text-orange-400">in configuration</p>
          </CardContent>
        </Card>
      </div>

      {/* Network Graph Section */}
      <div className="flex-1 mb-4">
        <Card className="h-full">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <span className="text-2xl">🌐</span>
              Network Visualization
            </CardTitle>
            <CardDescription>
              Interactive graph of rooms, agents, and teams relationships
            </CardDescription>
          </CardHeader>
          <CardContent className="p-2">
            <div className="w-full h-96">
              <NetworkGraph
                agents={filteredData.agents}
                rooms={filteredData.rooms}
                teams={filteredData.teams}
                selectedAgentId={selectedAgentId}
                selectedRoomId={selectedRoomId}
                onSelectAgent={agentId => {
                  selectAgent(agentId);
                  selectRoom(null);
                }}
                onSelectRoom={roomId => {
                  selectRoom(roomId);
                  selectAgent(null);
                }}
                width={1000}
                height={350}
              />
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Main Content Grid */}
      <div className="grid grid-cols-12 gap-4 min-h-0">
        {/* Agent Cards - Left Sidebar */}
        <div className="col-span-4">
          <Card className="h-full">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <span className="text-2xl">🤖</span>
                Agents
              </CardTitle>
              <CardDescription>Click an agent to see details</CardDescription>
            </CardHeader>
            <CardContent className="p-0">
              <ScrollArea className="h-[calc(100vh-400px)]">
                <div className="p-4 space-y-3">
                  {filteredData.agents.map(agent => (
                    <Card
                      key={agent.id}
                      className={`cursor-pointer transition-all hover:shadow-md ${
                        selectedAgentId === agent.id
                          ? 'ring-2 ring-primary bg-primary/5'
                          : 'hover:bg-gray-50 dark:hover:bg-gray-800'
                      }`}
                      onClick={() => {
                        selectAgent(agent.id);
                        selectRoom(null); // Clear room selection when selecting agent
                      }}
                    >
                      <CardContent className="p-4">
                        <div className="flex items-center justify-between mb-2">
                          <h3 className="font-semibold text-sm">{agent.display_name}</h3>
                          <div className="flex items-center gap-2">
                            {teams.some(team => team.agents.includes(agent.id)) && (
                              <Badge variant="secondary" className="text-xs px-1 py-0">
                                👥
                              </Badge>
                            )}
                            <div
                              className={`w-2 h-2 rounded-full ${getStatusColor(
                                getAgentStatus(agent.id)
                              )}`}
                              title={getStatusLabel(getAgentStatus(agent.id))}
                            />
                          </div>
                        </div>
                        <div className="text-xs text-gray-600 dark:text-gray-400 space-y-1">
                          <div>Model: {agent.model || 'Default'}</div>
                          <div>
                            Rooms: {agent.rooms.length} | Tools: {agent.tools.length}
                          </div>
                          <div className="flex flex-wrap gap-1 mt-2">
                            {agent.rooms.slice(0, 2).map(room => (
                              <Badge key={room} variant="secondary" className="text-xs px-1 py-0">
                                {room}
                              </Badge>
                            ))}
                            {agent.rooms.length > 2 && (
                              <Badge variant="outline" className="text-xs px-1 py-0">
                                +{agent.rooms.length - 2}
                              </Badge>
                            )}
                          </div>
                        </div>
                      </CardContent>
                    </Card>
                  ))}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>
        </div>

        {/* Center - Rooms Overview */}
        <div className="col-span-5 h-full">
          <Card className="h-full">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <span className="text-2xl">🏠</span>
                Rooms Overview
              </CardTitle>
              <CardDescription>Click a room to see details</CardDescription>
            </CardHeader>
            <CardContent className="p-0">
              <ScrollArea className="h-[calc(100vh-400px)]">
                <div className="p-4 space-y-3">
                  {filteredData.rooms.map(room => (
                    <Card
                      key={room.id}
                      className={`cursor-pointer transition-all hover:shadow-md ${
                        selectedRoomId === room.id
                          ? 'ring-2 ring-primary bg-primary/5'
                          : 'hover:bg-gray-50 dark:hover:bg-gray-800'
                      }`}
                      onClick={() => {
                        selectRoom(room.id);
                        selectAgent(null); // Clear agent selection when selecting room
                      }}
                    >
                      <CardContent className="p-4">
                        <div className="flex items-center justify-between mb-2">
                          <h3 className="font-semibold">{room.display_name}</h3>
                          <Badge variant="outline" className="text-xs">
                            {room.agents.length} agents
                          </Badge>
                        </div>
                        {room.model && (
                          <div className="text-xs text-gray-600 dark:text-gray-400 mb-2">
                            Model: {room.model}
                          </div>
                        )}
                        <div className="flex flex-wrap gap-1">
                          {room.agents.slice(0, 3).map(agentId => {
                            const agent = agents.find(a => a.id === agentId);
                            return (
                              <Badge
                                key={agentId}
                                variant="secondary"
                                className="text-xs px-1 py-0"
                              >
                                {agent?.display_name || agentId}
                              </Badge>
                            );
                          })}
                          {room.agents.length > 3 && (
                            <Badge variant="outline" className="text-xs px-1 py-0">
                              +{room.agents.length - 3}
                            </Badge>
                          )}
                        </div>

                        {/* Show teams in this room */}
                        {(() => {
                          const roomTeams = teams.filter(team => team.rooms.includes(room.id));
                          return roomTeams.length > 0 ? (
                            <div className="mt-2 pt-2 border-t border-gray-200 dark:border-gray-700">
                              <div className="text-xs text-gray-500 mb-1">Teams:</div>
                              <div className="flex flex-wrap gap-1">
                                {roomTeams.map(team => (
                                  <Badge
                                    key={team.id}
                                    variant="outline"
                                    className="text-xs px-1 py-0 bg-purple-50 dark:bg-purple-950"
                                  >
                                    👥 {team.display_name}
                                  </Badge>
                                ))}
                              </div>
                            </div>
                          ) : null;
                        })()}
                      </CardContent>
                    </Card>
                  ))}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>
        </div>

        {/* Right Panel - Selected Details */}
        <div className="col-span-3 h-full">
          <Card className="h-full">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <span className="text-2xl">ℹ️</span>
                Details
              </CardTitle>
            </CardHeader>
            <CardContent>
              {selectedRoom ? (
                <div className="space-y-4">
                  <div>
                    <h3 className="font-semibold text-lg mb-2">{selectedRoom.display_name}</h3>
                    {selectedRoom.description && (
                      <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
                        {selectedRoom.description}
                      </p>
                    )}
                  </div>

                  {selectedRoom.model && (
                    <div>
                      <h4 className="font-medium mb-1">Model Override:</h4>
                      <Badge variant="secondary">{selectedRoom.model}</Badge>
                    </div>
                  )}

                  <div>
                    <h4 className="font-medium mb-2">Agents ({selectedRoom.agents.length}):</h4>
                    <div className="space-y-2">
                      {selectedRoom.agents.map(agentId => {
                        const agent = agents.find(a => a.id === agentId);
                        if (!agent) return null;
                        return (
                          <div
                            key={agentId}
                            className="flex items-center justify-between p-2 bg-gray-50 dark:bg-gray-800 rounded text-sm"
                          >
                            <span>{agent.display_name}</span>
                            <div className="flex items-center gap-2">
                              <Badge variant="outline" className="text-xs">
                                {agent.tools.length} tools
                              </Badge>
                              <div
                                className={`w-2 h-2 rounded-full ${getStatusColor(
                                  getAgentStatus(agent.id)
                                )}`}
                                title={getStatusLabel(getAgentStatus(agent.id))}
                              />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  {(() => {
                    const roomTeams = teams.filter(team => team.rooms.includes(selectedRoom.id));
                    return roomTeams.length > 0 ? (
                      <div>
                        <h4 className="font-medium mb-2">Teams ({roomTeams.length}):</h4>
                        <div className="space-y-2">
                          {roomTeams.map(team => (
                            <div
                              key={team.id}
                              className="p-2 bg-purple-50 dark:bg-purple-950 rounded text-sm"
                            >
                              <div className="font-medium">👥 {team.display_name}</div>
                              <div className="text-xs text-gray-600 dark:text-gray-400">
                                {team.mode} mode • {team.agents.length} members
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    ) : null;
                  })()}
                </div>
              ) : selectedAgent ? (
                <div className="space-y-4">
                  <div>
                    <h3 className="font-semibold text-lg mb-2">{selectedAgent.display_name}</h3>
                    <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
                      {selectedAgent.role}
                    </p>
                  </div>

                  <div>
                    <h4 className="font-medium mb-1">Model:</h4>
                    <Badge variant="secondary">{selectedAgent.model || 'Default'}</Badge>
                  </div>

                  <div>
                    <h4 className="font-medium mb-2">Rooms ({selectedAgent.rooms.length}):</h4>
                    <div className="flex flex-wrap gap-1">
                      {selectedAgent.rooms.map(roomId => (
                        <Badge key={roomId} variant="outline" className="text-xs">
                          {roomId}
                        </Badge>
                      ))}
                    </div>
                  </div>

                  <div>
                    <h4 className="font-medium mb-2">Tools ({selectedAgent.tools.length}):</h4>
                    <div className="flex flex-wrap gap-1">
                      {selectedAgent.tools.slice(0, 8).map(tool => (
                        <Badge key={tool} variant="secondary" className="text-xs">
                          {tool}
                        </Badge>
                      ))}
                      {selectedAgent.tools.length > 8 && (
                        <Badge variant="outline" className="text-xs">
                          +{selectedAgent.tools.length - 8}
                        </Badge>
                      )}
                    </div>
                  </div>

                  {(() => {
                    const agentTeams = teams.filter(team => team.agents.includes(selectedAgent.id));
                    return agentTeams.length > 0 ? (
                      <div>
                        <h4 className="font-medium mb-2">Team Memberships:</h4>
                        <div className="space-y-1">
                          {agentTeams.map(team => (
                            <Badge key={team.id} variant="outline" className="text-xs block w-fit">
                              👥 {team.display_name}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    ) : null;
                  })()}
                </div>
              ) : (
                <div className="text-center text-gray-500 dark:text-gray-400 mt-8">
                  <p>Select a room or agent to see details</p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
