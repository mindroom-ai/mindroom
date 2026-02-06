import { useCallback, useEffect, useRef, useState } from 'react';
import Editor from '@monaco-editor/react';
import { ArrowLeft, FileCode, FolderOpen, Lock, Pencil, Save, Trash2 } from 'lucide-react';
import { ListPanel, ListItem } from '@/components/shared/ListPanel';
import { ItemCard, ItemCardBadge } from '@/components/shared/ItemCard';
import { EditorPanelEmptyState } from '@/components/shared';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useToast } from '@/components/ui/use-toast';
import { useTheme } from '@/contexts/ThemeContext';
import { useSwipeBack } from '@/hooks/useSwipeBack';
import {
  createSkill,
  deleteSkill,
  getSkill,
  listSkills,
  updateSkill,
} from '@/services/skillsService';
import { SkillSummary } from '@/types/skills';

interface SkillListItem extends ListItem {
  description: string;
  origin: SkillSummary['origin'];
  can_edit: boolean;
}

export function Skills() {
  const { toast } = useToast();
  const toastRef = useRef(toast);
  toastRef.current = toast;

  const { resolvedTheme } = useTheme();
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [draftContent, setDraftContent] = useState('');
  const [originalContent, setOriginalContent] = useState('');
  const [loadingList, setLoadingList] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [saving, setSaving] = useState(false);

  const selectedSkill = selectedName ? skills.find(s => s.name === selectedName) ?? null : null;
  const isDirty = draftContent !== originalContent;

  useSwipeBack({
    onSwipeBack: () => setSelectedName(null),
    enabled: !!selectedSkill && window.innerWidth < 1024,
  });

  const refreshSkills = useCallback(async () => {
    setLoadingList(true);
    try {
      const data = await listSkills();
      setSkills(data);
      setSelectedName(current => (current && data.some(s => s.name === current) ? current : null));
    } catch (error) {
      toastRef.current({
        title: 'Failed to load skills',
        description: error instanceof Error ? error.message : 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => {
    refreshSkills();
  }, [refreshSkills]);

  useEffect(() => {
    let isActive = true;

    if (!selectedName) {
      setDraftContent('');
      setOriginalContent('');
      return;
    }

    setLoadingDetail(true);
    getSkill(selectedName)
      .then(detail => {
        if (!isActive) return;
        setDraftContent(detail.content);
        setOriginalContent(detail.content);
      })
      .catch(error => {
        if (!isActive) return;
        toastRef.current({
          title: 'Failed to load skill',
          description: error instanceof Error ? error.message : 'Unknown error',
          variant: 'destructive',
        });
      })
      .finally(() => {
        setLoadingDetail(false);
      });

    return () => {
      isActive = false;
    };
  }, [selectedName]);

  const handleSelect = (name: string) => {
    if (isDirty && selectedName !== name) {
      if (!window.confirm('Discard unsaved changes?')) return;
    }
    setSelectedName(name);
  };

  const handleSave = async () => {
    if (!selectedSkill) return;
    setSaving(true);
    try {
      await updateSkill(selectedSkill.name, draftContent);
      setOriginalContent(draftContent);
      toast({ title: 'Skill saved', description: `${selectedSkill.name} updated.` });
      await refreshSkills();
    } catch (error) {
      toast({
        title: 'Failed to save skill',
        description: error instanceof Error ? error.message : 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setSaving(false);
    }
  };

  const handleCreate = async (name?: string) => {
    if (!name) return;
    try {
      await createSkill(name, name);
      toast({ title: 'Skill created', description: `${name} is ready to edit.` });
      await refreshSkills();
      setSelectedName(name);
    } catch (error) {
      toast({
        title: 'Failed to create skill',
        description: error instanceof Error ? error.message : 'Unknown error',
        variant: 'destructive',
      });
    }
  };

  const handleDelete = async () => {
    if (!selectedSkill) return;
    if (!window.confirm(`Delete skill "${selectedSkill.name}"? This cannot be undone.`)) return;
    try {
      await deleteSkill(selectedSkill.name);
      toast({ title: 'Skill deleted', description: `${selectedSkill.name} removed.` });
      setSelectedName(null);
      await refreshSkills();
    } catch (error) {
      toast({
        title: 'Failed to delete skill',
        description: error instanceof Error ? error.message : 'Unknown error',
        variant: 'destructive',
      });
    }
  };

  const listItems: SkillListItem[] = skills.map(s => ({
    id: s.name,
    display_name: s.name,
    description: s.description,
    origin: s.origin,
    can_edit: s.can_edit,
  }));

  const renderSkill = (skill: SkillListItem, isSelected: boolean) => {
    const badges: ItemCardBadge[] = [
      { content: skill.origin, variant: 'secondary' as const, icon: FolderOpen },
      {
        content: skill.can_edit ? 'Editable' : 'Read-only',
        variant: skill.can_edit ? ('default' as const) : ('outline' as const),
        icon: skill.can_edit ? Pencil : Lock,
      },
    ];

    return (
      <ItemCard
        id={skill.id}
        title={skill.display_name}
        description={skill.description}
        isSelected={isSelected}
        onClick={handleSelect}
        badges={badges}
      />
    );
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 sm:gap-4 h-full">
      <div
        className={`col-span-1 lg:col-span-4 h-full overflow-hidden ${
          selectedSkill ? 'hidden lg:block' : 'block'
        }`}
      >
        <ListPanel<SkillListItem>
          title="Skills"
          icon={FileCode}
          items={listItems}
          selectedId={selectedName || undefined}
          onItemSelect={handleSelect}
          renderItem={renderSkill}
          showSearch={true}
          searchPlaceholder="Search skills..."
          showCreateButton={true}
          creationMode="inline-form"
          createPlaceholder="Skill name..."
          createButtonText="New"
          onCreateItem={handleCreate}
          emptyIcon={FileCode}
          emptyMessage={loadingList ? 'Loading skills...' : 'No skills found'}
          emptySubtitle={
            loadingList
              ? 'Fetching skill list from the server'
              : 'Add skills under ~/.mindroom/skills to get started'
          }
        />
      </div>
      <div
        className={`col-span-1 lg:col-span-8 h-full overflow-hidden ${
          selectedSkill ? 'block' : 'hidden lg:block'
        }`}
      >
        {!selectedSkill ? (
          <EditorPanelEmptyState icon={FileCode} message="Select a skill to view" />
        ) : (
          <Card className="h-full flex flex-col overflow-hidden">
            <CardHeader className="pb-3 flex-shrink-0">
              <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setSelectedName(null)}
                    className="lg:hidden -ml-2 mt-1"
                  >
                    <ArrowLeft className="h-4 w-4" />
                  </Button>
                  <div>
                    <CardTitle className="flex items-center gap-2">
                      <FileCode className="h-5 w-5" />
                      {selectedSkill.name}
                    </CardTitle>
                    <p className="text-sm text-muted-foreground mt-1">
                      {selectedSkill.description}
                    </p>
                  </div>
                </div>
                {selectedSkill.can_edit && (
                  <div className="flex items-center gap-1">
                    <Button
                      variant="default"
                      size="sm"
                      onClick={handleSave}
                      disabled={!isDirty || saving}
                    >
                      <Save className="h-4 w-4 sm:mr-1" />
                      <span className="hidden sm:inline">{saving ? 'Saving...' : 'Save'}</span>
                    </Button>
                    <Button variant="ghost" size="sm" onClick={handleDelete}>
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2 mt-3">
                <Badge variant="secondary">{selectedSkill.origin}</Badge>
                {!selectedSkill.can_edit && <Badge variant="outline">Read-only</Badge>}
              </div>
            </CardHeader>
            <CardContent className="flex-1 min-h-0 p-0">
              {loadingDetail ? (
                <div className="h-full flex items-center justify-center text-sm text-muted-foreground">
                  Loading skill contents...
                </div>
              ) : (
                <Editor
                  height="100%"
                  language="markdown"
                  theme={resolvedTheme === 'dark' ? 'vs-dark' : 'vs'}
                  value={draftContent}
                  onChange={value => setDraftContent(value ?? '')}
                  loading={
                    <div className="h-full flex items-center justify-center text-sm text-muted-foreground">
                      Loading editor...
                    </div>
                  }
                  options={{
                    readOnly: !selectedSkill.can_edit,
                    minimap: { enabled: false },
                    fontSize: 13,
                    scrollBeyondLastLine: false,
                    wordWrap: 'on',
                    padding: { top: 12, bottom: 12 },
                  }}
                />
              )}
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
