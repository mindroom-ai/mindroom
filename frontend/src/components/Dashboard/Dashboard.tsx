import { useState, useMemo, useEffect, useCallback } from 'react';
import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { NetworkGraph } from './NetworkGraph';
import { ItemCard, ItemCardBadge } from '@/components/shared/ItemCard';
import { sharedStyles } from '@/components/shared/styles';
import { FilterSelector } from '@/components/shared/FilterSelector';
import {
  Bot,
  Home,
  Users,
  Settings,
  RefreshCw,
  FileText,
  BarChart3,
  User,
  Mic,
  MicOff,
} from 'lucide-react';

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
      voiceEnabled: config?.voice?.enabled || false,
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
    <div className="flex flex-col gap-4">
      {/* Header with Quick Actions */}
      <div className="flex flex-col sm:flex-row sm:justify-between sm:items-center gap-3">
        <div>
          <h2 className="text-xl sm:text-2xl font-bold">System Overview</h2>
          <p className="text-sm sm:text-base text-amber-700 dark:text-amber-300">
            Monitor your MindRoom configuration and status
          </p>
          <p className="text-xs text-amber-600 dark:text-amber-400 mt-1 flex items-center gap-1">
            <RefreshCw className="w-3 h-3" /> Last updated: {lastUpdated.toLocaleTimeString()}
          </p>
        </div>
        <div className="flex flex-wrap gap-2 items-center">
          <Input
            placeholder="Search..."
            value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
            className="w-32 sm:w-48 md:w-64"
          />
          <FilterSelector
            options={[
              {
                value: 'agents',
                label: (
                  <>
                    <Bot className="w-4 h-4" />
                    <span className="hidden lg:inline">Agents</span>
                  </>
                ),
              },
              {
                value: 'rooms',
                label: (
                  <>
                    <Home className="w-4 h-4" />
                    <span className="hidden lg:inline">Rooms</span>
                  </>
                ),
              },
              {
                value: 'teams',
                label: (
                  <>
                    <Users className="w-4 h-4" />
                    <span className="hidden lg:inline">Teams</span>
                  </>
                ),
              },
            ]}
            value={showTypes}
            onChange={value => setShowTypes(value as string[])}
            multiple
            className="hidden md:inline-flex"
          />
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              selectAgent(null);
              selectRoom(null);
            }}
            className="hidden sm:flex"
          >
            Clear Selection
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={exportConfiguration}
            className="flex items-center gap-1"
          >
            <FileText className="w-4 h-4" /> <span className="hidden sm:inline">Export Config</span>
          </Button>
        </div>
      </div>

      {/* System Stats Cards - Top Bar */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 sm:gap-4">
        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-amber-100 dark:bg-yellow-900/30">
                <Bot className="w-5 h-5 text-amber-700 dark:text-amber-300" />
              </div>
              <div>
                <CardTitle className="text-2xl font-bold text-amber-900 dark:text-amber-100">
                  {stats.totalAgents}
                </CardTitle>
                <CardDescription className="text-amber-700 dark:text-amber-300">
                  Agents
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-3 text-xs text-amber-700 dark:text-amber-300">
              <span className="flex items-center gap-1">
                <div className="w-2 h-2 rounded-full bg-green-500"></div> {stats.agentsOnline}
              </span>
              <span className="flex items-center gap-1">
                <div className="w-2 h-2 rounded-full bg-orange-500"></div> {stats.agentsBusy}
              </span>
              <span className="flex items-center gap-1">
                <div className="w-2 h-2 rounded-full bg-yellow-500"></div> {stats.agentsIdle}
              </span>
              <span className="flex items-center gap-1">
                <div className="w-2 h-2 rounded-full bg-gray-400"></div> {stats.agentsOffline}
              </span>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-orange-100 dark:bg-orange-900/30">
                <Home className="w-5 h-5 text-orange-700 dark:text-orange-300" />
              </div>
              <div>
                <CardTitle className="text-2xl font-bold text-orange-900 dark:text-orange-100">
                  {stats.totalRooms}
                </CardTitle>
                <CardDescription className="text-orange-700 dark:text-orange-300">
                  Rooms
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-orange-700 dark:text-orange-300">
              {stats.activeConnections} configured
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-yellow-100 dark:bg-yellow-900/30">
                <Users className="w-5 h-5 text-yellow-700 dark:text-yellow-300" />
              </div>
              <div>
                <CardTitle className="text-2xl font-bold text-yellow-900 dark:text-yellow-100">
                  {stats.totalTeams}
                </CardTitle>
                <CardDescription className="text-yellow-700 dark:text-yellow-300">
                  Teams
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-yellow-700 dark:text-yellow-300">
              {teams.reduce((acc, team) => acc + team.agents.length, 0)} members
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-stone-100 dark:bg-stone-900/30">
                <Settings className="w-5 h-5 text-stone-700 dark:text-stone-300" />
              </div>
              <div>
                <CardTitle className="text-2xl font-bold text-stone-900 dark:text-stone-100">
                  {stats.modelsInUse}
                </CardTitle>
                <CardDescription className="text-stone-700 dark:text-stone-300">
                  Models
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-stone-700 dark:text-stone-300">in configuration</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <div className="flex items-center gap-3">
              <div
                className={`p-2 rounded-lg ${
                  stats.voiceEnabled
                    ? 'bg-purple-100 dark:bg-purple-900/30'
                    : 'bg-gray-100 dark:bg-gray-900/30'
                }`}
              >
                {stats.voiceEnabled ? (
                  <Mic className="w-5 h-5 text-purple-700 dark:text-purple-300" />
                ) : (
                  <MicOff className="w-5 h-5 text-gray-500 dark:text-gray-400" />
                )}
              </div>
              <div>
                <CardTitle className="text-2xl font-bold text-purple-900 dark:text-purple-100">
                  Voice
                </CardTitle>
                <CardDescription
                  className={
                    stats.voiceEnabled
                      ? 'text-purple-700 dark:text-purple-300'
                      : 'text-gray-500 dark:text-gray-400'
                  }
                >
                  {stats.voiceEnabled ? 'Enabled' : 'Disabled'}
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <p
              className={`text-xs ${
                stats.voiceEnabled
                  ? 'text-purple-700 dark:text-purple-300'
                  : 'text-gray-500 dark:text-gray-400'
              }`}
            >
              {stats.voiceEnabled ? 'Transcription active' : 'Configure in Voice tab'}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Network Graph Section */}
      <div className="mb-4 hidden lg:block">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-amber-900 dark:text-amber-100">
              <BarChart3 className="w-6 h-6 text-amber-700 dark:text-amber-300" />
              System Insights
            </CardTitle>
            <CardDescription className="text-amber-700 dark:text-amber-300">
              Key metrics and actionable insights about your MindRoom configuration
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
                onSelectAgent={(agentId: string | null) => {
                  selectAgent(agentId);
                  selectRoom(null);
                }}
                onSelectRoom={(roomId: string | null) => {
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
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
        {/* Agent Cards - Left Sidebar */}
        <div className="col-span-1 lg:col-span-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-amber-900 dark:text-amber-100">
                <Bot className="w-6 h-6 text-amber-700 dark:text-amber-300" />
                Agents
              </CardTitle>
              <CardDescription className="text-amber-700 dark:text-amber-300">
                Click an agent to see details
              </CardDescription>
            </CardHeader>
            <CardContent className="p-0 flex-1 overflow-y-auto min-h-0">
              <ScrollArea className="h-96">
                <div className={`${sharedStyles.list.containerWithSpacing} p-3 sm:p-4`}>
                  {filteredData.agents.map(agent => {
                    const badges: ItemCardBadge[] = [];
                    const agentTeams = teams.filter(team => team.agents.includes(agent.id));

                    if (agentTeams.length > 0) {
                      badges.push({
                        content: `${agentTeams.length} team${agentTeams.length === 1 ? '' : 's'}`,
                        variant: 'secondary' as const,
                        icon: Users,
                      });
                    }

                    return (
                      <ItemCard
                        key={agent.id}
                        id={agent.id}
                        title={agent.display_name}
                        description={`Model: ${agent.model || 'Default'} • ${
                          agent.rooms.length
                        } rooms • ${agent.tools.length} tools`}
                        isSelected={selectedAgentId === agent.id}
                        onClick={id => {
                          selectAgent(id);
                          selectRoom(null);
                        }}
                        badges={badges}
                      >
                        <div className="flex items-center justify-between mt-2">
                          <div className="flex flex-wrap gap-1">
                            {agent.rooms.slice(0, 2).map(room => (
                              <Badge
                                key={room}
                                variant="secondary"
                                className={sharedStyles.badge.secondary}
                              >
                                {room}
                              </Badge>
                            ))}
                            {agent.rooms.length > 2 && (
                              <Badge variant="outline" className={sharedStyles.badge.outline}>
                                +{agent.rooms.length - 2}
                              </Badge>
                            )}
                          </div>
                          <div
                            className={`w-2 h-2 rounded-full ${getStatusColor(
                              getAgentStatus(agent.id)
                            )}`}
                            title={getStatusLabel(getAgentStatus(agent.id))}
                          />
                        </div>
                      </ItemCard>
                    );
                  })}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>
        </div>

        {/* Center - Rooms Overview */}
        <div className="col-span-1 lg:col-span-5">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-amber-900 dark:text-amber-100">
                <Home className="w-6 h-6 text-amber-700 dark:text-amber-300" />
                Rooms Overview
              </CardTitle>
              <CardDescription className="text-amber-700 dark:text-amber-300">
                Click a room to see details
              </CardDescription>
            </CardHeader>
            <CardContent className="p-0 flex-1 overflow-y-auto min-h-0">
              <ScrollArea className="h-96">
                <div className={`${sharedStyles.list.containerWithSpacing} p-3 sm:p-4`}>
                  {filteredData.rooms.map(room => {
                    const badges: ItemCardBadge[] = [
                      {
                        content: `${room.agents.length} agents`,
                        variant: 'outline' as const,
                        icon: Bot,
                      },
                    ];

                    if (room.model) {
                      badges.push({
                        content: `Model: ${room.model}`,
                        variant: 'secondary' as const,
                      });
                    }

                    const roomTeams = teams.filter(team => team.rooms.includes(room.id));
                    if (roomTeams.length > 0) {
                      badges.push({
                        content: `${roomTeams.length} teams`,
                        variant: 'outline' as const,
                        icon: Users,
                      });
                    }

                    return (
                      <ItemCard
                        key={room.id}
                        id={room.id}
                        title={room.display_name}
                        description={
                          room.model
                            ? `Model: ${room.model} • ${room.agents.length} agents`
                            : `${room.agents.length} agents`
                        }
                        isSelected={selectedRoomId === room.id}
                        onClick={id => {
                          selectRoom(id);
                          selectAgent(null);
                        }}
                        badges={badges}
                      >
                        <div className="flex flex-wrap gap-1 mt-2">
                          {room.agents.slice(0, 3).map(agentId => {
                            const agent = agents.find(a => a.id === agentId);
                            return (
                              <Badge
                                key={agentId}
                                variant="secondary"
                                className={sharedStyles.badge.secondary}
                              >
                                {agent?.display_name || agentId}
                              </Badge>
                            );
                          })}
                          {room.agents.length > 3 && (
                            <Badge variant="outline" className={sharedStyles.badge.outline}>
                              +{room.agents.length - 3}
                            </Badge>
                          )}
                        </div>

                        {roomTeams.length > 0 && (
                          <div className="mt-2 pt-2 border-t border-gray-200 dark:border-gray-700">
                            <div className="text-xs text-amber-600 dark:text-amber-400 mb-1">
                              Teams:
                            </div>
                            <div className="flex flex-wrap gap-1">
                              {roomTeams.map(team => (
                                <Badge
                                  key={team.id}
                                  variant="outline"
                                  className="text-xs px-1 py-0 bg-purple-50 dark:bg-purple-950 flex items-center gap-1"
                                >
                                  <Users className="w-3 h-3" /> {team.display_name}
                                </Badge>
                              ))}
                            </div>
                          </div>
                        )}
                      </ItemCard>
                    );
                  })}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>
        </div>

        {/* Right Panel - Selected Details */}
        <div className="col-span-1 lg:col-span-3">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-amber-900 dark:text-amber-100">
                <User className="w-6 h-6 text-amber-700 dark:text-amber-300" />
                Details
              </CardTitle>
            </CardHeader>
            <CardContent>
              {selectedRoom ? (
                <div className="space-y-4">
                  <div>
                    <h3 className="font-semibold text-lg mb-2">{selectedRoom.display_name}</h3>
                    {selectedRoom.description && (
                      <p className="text-sm text-amber-700 dark:text-amber-300 mb-3">
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
                              <div className="font-medium flex items-center gap-1">
                                <Users className="w-4 h-4" /> {team.display_name}
                              </div>
                              <div className="text-xs text-amber-700 dark:text-amber-300">
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
                    <p className="text-sm text-amber-700 dark:text-amber-300 mb-3">
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
                            <Badge
                              key={team.id}
                              variant="outline"
                              className="text-xs block w-fit flex items-center gap-1"
                            >
                              <Users className="w-3 h-3" /> {team.display_name}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    ) : null;
                  })()}
                </div>
              ) : (
                <div className="text-center text-amber-600 dark:text-amber-400 dark:text-gray-400 mt-8">
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
