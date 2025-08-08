import { useConfigStore } from '@/store/configStore';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Plus, Users, Search } from 'lucide-react';
import { useState } from 'react';
import { cn } from '@/lib/utils';

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
      <CardContent className="flex-1 overflow-hidden p-0">
        <ScrollArea className="h-full px-4">
          {isCreating && (
            <div className="mb-2 p-3 border rounded-lg bg-blue-50">
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
                className="mb-2"
              />
              <div className="flex gap-2">
                <Button size="sm" onClick={handleCreateTeam}>
                  Create
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setIsCreating(false);
                    setNewTeamName('');
                  }}
                >
                  Cancel
                </Button>
              </div>
            </div>
          )}
          <div className="space-y-2 pb-4">
            {filteredTeams.map(team => (
              <button
                key={team.id}
                onClick={() => selectTeam(team.id)}
                className={cn(
                  'w-full text-left p-3 rounded-lg transition-all duration-200',
                  'hover:shadow-md hover:scale-[1.02]',
                  selectedTeamId === team.id
                    ? 'bg-gradient-to-r from-blue-500 to-purple-500 text-white shadow-lg'
                    : 'bg-white border border-gray-200 hover:border-blue-300'
                )}
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <h3
                      className={cn(
                        'font-semibold',
                        selectedTeamId === team.id ? 'text-white' : 'text-gray-900'
                      )}
                    >
                      {team.display_name}
                    </h3>
                    <p
                      className={cn(
                        'text-sm mt-1 line-clamp-2',
                        selectedTeamId === team.id ? 'text-blue-100' : 'text-gray-600'
                      )}
                    >
                      {team.role}
                    </p>
                    <div className="flex items-center gap-4 mt-2">
                      <span
                        className={cn(
                          'text-xs',
                          selectedTeamId === team.id ? 'text-blue-100' : 'text-gray-500'
                        )}
                      >
                        {team.agents.length} agents
                      </span>
                      <span
                        className={cn(
                          'text-xs px-2 py-0.5 rounded-full',
                          selectedTeamId === team.id
                            ? 'bg-white/20 text-white'
                            : 'bg-gray-100 text-gray-600'
                        )}
                      >
                        {team.mode}
                      </span>
                    </div>
                  </div>
                </div>
              </button>
            ))}
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  );
}
