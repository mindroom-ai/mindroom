import { useCallback, useEffect, useMemo, useState } from 'react';
import Editor, { loader } from '@monaco-editor/react';
import { ArrowLeft, FileCode, FolderOpen, Lock, Pencil, Save, ShieldAlert } from 'lucide-react';
import { ListPanel, ListItem } from '@/components/shared/ListPanel';
import { ItemCard, ItemCardBadge } from '@/components/shared/ItemCard';
import { EditorPanelEmptyState } from '@/components/shared';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useToast } from '@/components/ui/use-toast';
import { useTheme } from '@/contexts/ThemeContext';
import { useSwipeBack } from '@/hooks/useSwipeBack';
import { getSkill, listSkills, updateSkill } from '@/services/skillsService';
import { SkillSummary } from '@/types/skills';

interface SkillListItem extends ListItem {
  description: string;
  origin: SkillSummary['origin'];
  can_edit: boolean;
  path: string;
  name: string;
}

loader.config({
  paths: { vs: '/monaco/vs' },
});

export function Skills() {
  const { toast } = useToast();
  const { resolvedTheme } = useTheme();
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [selectedSkillPath, setSelectedSkillPath] = useState<string | null>(null);
  const [draftContent, setDraftContent] = useState('');
  const [originalContent, setOriginalContent] = useState('');
  const [loadingList, setLoadingList] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [saving, setSaving] = useState(false);

  const selectedSkill = useMemo(
    () => skills.find(skill => skill.path === selectedSkillPath) || null,
    [skills, selectedSkillPath]
  );

  const isDirty = draftContent !== originalContent;

  useSwipeBack({
    onSwipeBack: () => setSelectedSkillPath(null),
    enabled: !!selectedSkill && window.innerWidth < 1024,
  });

  const refreshSkills = useCallback(async () => {
    setLoadingList(true);
    try {
      const data = await listSkills();
      setSkills(data);
      setSelectedSkillPath(current =>
        current && data.some(skill => skill.path === current) ? current : null
      );
    } catch (error) {
      toast({
        title: 'Failed to load skills',
        description: error instanceof Error ? error.message : 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setLoadingList(false);
    }
  }, [toast]);

  useEffect(() => {
    refreshSkills();
  }, [refreshSkills]);

  useEffect(() => {
    let isActive = true;

    const loadDetail = async () => {
      if (!selectedSkill) {
        setDraftContent('');
        setOriginalContent('');
        return;
      }

      setLoadingDetail(true);
      try {
        const detail = await getSkill(selectedSkill.name);
        if (!isActive) return;
        setDraftContent(detail.content);
        setOriginalContent(detail.content);
      } catch (error) {
        if (!isActive) return;
        toast({
          title: 'Failed to load skill',
          description: error instanceof Error ? error.message : 'Unknown error',
          variant: 'destructive',
        });
      } finally {
        if (isActive) {
          setLoadingDetail(false);
        }
      }
    };

    loadDetail();
    return () => {
      isActive = false;
    };
  }, [selectedSkill, toast]);

  const handleSelect = (skillId: string) => {
    const nextSkill = skills.find(skill => skill.path === skillId);
    if (!nextSkill) {
      return;
    }
    if (isDirty && selectedSkillPath !== skillId) {
      const confirmLeave = window.confirm('Discard unsaved changes?');
      if (!confirmLeave) {
        return;
      }
    }
    setSelectedSkillPath(skillId);
  };

  const handleSave = async () => {
    if (!selectedSkill) {
      return;
    }
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

  const listItems: SkillListItem[] = useMemo(
    () =>
      skills.map(skill => ({
        id: skill.path,
        display_name: skill.name,
        description: skill.description,
        origin: skill.origin,
        can_edit: skill.can_edit,
        path: skill.path,
        name: skill.name,
      })),
    [skills]
  );

  const renderSkill = (skill: SkillListItem, isSelected: boolean) => {
    const badges: ItemCardBadge[] = [
      {
        content: skill.origin,
        variant: 'secondary' as const,
        icon: FolderOpen,
      },
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
      >
        <div className="text-xs text-muted-foreground truncate" title={skill.path}>
          {skill.path}
        </div>
      </ItemCard>
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
          selectedId={selectedSkillPath || undefined}
          onItemSelect={handleSelect}
          renderItem={renderSkill}
          showSearch={true}
          searchPlaceholder="Search skills..."
          showCreateButton={false}
          emptyIcon={loadingList ? ShieldAlert : FileCode}
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
          <EditorPanelEmptyState icon={FileCode} message="Select a skill to edit" />
        ) : (
          <Card className="h-full flex flex-col overflow-hidden">
            <CardHeader className="pb-3 flex-shrink-0">
              <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setSelectedSkillPath(null)}
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
                <Button
                  variant="default"
                  size="sm"
                  onClick={handleSave}
                  disabled={!isDirty || !selectedSkill.can_edit || saving}
                >
                  <Save className="h-4 w-4 sm:mr-1" />
                  <span className="hidden sm:inline">{saving ? 'Saving...' : 'Save'}</span>
                </Button>
              </div>
              <div className="flex flex-wrap items-center gap-2 mt-3">
                <Badge variant="secondary">{selectedSkill.origin}</Badge>
                <Badge variant={selectedSkill.can_edit ? 'default' : 'outline'}>
                  {selectedSkill.can_edit ? 'Editable' : 'Read-only'}
                </Badge>
                {!selectedSkill.can_edit && (
                  <span className="text-xs text-muted-foreground">
                    This skill is read-only in the current environment.
                  </span>
                )}
              </div>
              <div
                className="text-xs text-muted-foreground mt-2 truncate"
                title={selectedSkill.path}
              >
                {selectedSkill.path}
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
                  options={{
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
