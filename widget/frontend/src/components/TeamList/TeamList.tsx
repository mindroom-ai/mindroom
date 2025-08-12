import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Plus, Users, Search } from 'lucide-react';
import { useState } from 'react';

export function TeamList() {
  const { teams, selectedTeamId, selectTeam, createTeam } = useConfigStore();
  const [searchTerm, setSearchTerm] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [newTeamName, setNewTeamName] = useState('');

  const filteredTeams = teams.filter(
    team =>
      team.display_name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      team.role.toLowerCase().includes(searchTerm.toLowerCase())
  );

  const handleCreateTeam = () => {
    if (newTeamName.trim()) {
      createTeam({
        display_name: newTeamName,
        role: 'New team description',
        agents: [],
        rooms: [],
        mode: 'coordinate',
      });
      setNewTeamName('');
      setIsCreating(false);
    }
  };

  return (
    <Card className="h-full flex flex-col">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            Teams
          </CardTitle>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setIsCreating(true)}
            className="h-8 w-8 p-0"
          >
            <Plus className="h-4 w-4" />
          </Button>
        </div>
        <div className="relative mt-2">
          <Search className="absolute left-2 top-2.5 h-4 w-4 text-gray-400" />
          <Input
            placeholder="Search teams..."
            value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
            className="pl-8 h-9"
          />
        </div>
      </CardHeader>
      <div className="flex-1 overflow-y-auto p-4 space-y-2">
        {isCreating && (
          <Card className="border-2 border-orange-500">
            <CardContent className="p-3">
              <div className="flex items-center gap-2">
                <Input
                  placeholder="Team name..."
                  value={newTeamName}
                  onChange={e => setNewTeamName(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') handleCreateTeam();
                    if (e.key === 'Escape') {
                      setIsCreating(false);
                      setNewTeamName('');
                    }
                  }}
                  autoFocus
                  className="flex-1"
                />
                <Button size="sm" onClick={handleCreateTeam} variant="default">
                  Create
                </Button>
                <Button
                  size="sm"
                  onClick={() => {
                    setIsCreating(false);
                    setNewTeamName('');
                  }}
                  variant="ghost"
                >
                  Cancel
                </Button>
              </div>
            </CardContent>
          </Card>
        )}

        {filteredTeams.length === 0 && !isCreating ? (
          <div className="text-center py-8 text-muted-foreground">
            <Users className="h-12 w-12 mx-auto mb-3 opacity-20" />
            <p className="text-sm">No teams found</p>
            <p className="text-xs mt-1">Click "+" to create one</p>
          </div>
        ) : (
          filteredTeams.map(team => (
            <Card
              key={team.id}
              className={`cursor-pointer transition-all hover:shadow-md hover:scale-[1.01] ${
                selectedTeamId === team.id
                  ? 'ring-2 ring-orange-500 bg-gradient-to-r from-orange-500/10 to-amber-500/10'
                  : ''
              }`}
              onClick={() => selectTeam(team.id)}
            >
              <CardContent className="p-4">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <h3 className="font-medium text-sm">{team.display_name}</h3>
                    <p className="text-xs text-muted-foreground mt-1">{team.role}</p>
                    <div className="flex items-center gap-2 mt-2">
                      <Badge variant="secondary" className="text-xs">
                        <Users className="h-3 w-3 mr-1" />
                        {team.agents.length} agents
                      </Badge>
                      <Badge variant="outline" className="text-xs">
                        Mode: {team.mode}
                      </Badge>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          ))
        )}
      </div>
    </Card>
  );
}
