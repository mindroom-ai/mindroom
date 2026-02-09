import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CalendarClock,
  Clock3,
  Loader2,
  MapPin,
  MessageSquare,
  RefreshCw,
  Repeat,
  Timer,
} from 'lucide-react';
import { useSwipeBack } from '@/hooks/useSwipeBack';
import { cancelSchedule, listSchedules, updateSchedule } from '@/services/scheduleService';
import type { ScheduleTask, ScheduleType, UpdateScheduleRequest } from '@/types/schedule';
import { useToast } from '@/components/ui/use-toast';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import {
  EditorPanel,
  EditorPanelEmptyState,
  FieldGroup,
  ItemCard,
  type ItemCardBadge,
  ListPanel,
  type ListItem,
} from '@/components/shared';

interface ScheduleListItem extends ListItem, ScheduleTask {
  id: string;
  display_name: string;
}

interface ScheduleDraft {
  task_id: string;
  room_id: string;
  message: string;
  description: string;
  schedule_type: ScheduleType;
  execute_at_input: string;
  cron_expression: string;
}

type DateParts = {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
};

function parseDateTimeInput(localDateTime: string): DateParts | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(localDateTime);
  if (!match) return null;

  const [, year, month, day, hour, minute] = match;
  return {
    year: Number(year),
    month: Number(month),
    day: Number(day),
    hour: Number(hour),
    minute: Number(minute),
    second: 0,
  };
}

function formatDateTimeInput(parts: DateParts): string {
  const year = String(parts.year).padStart(4, '0');
  const month = String(parts.month).padStart(2, '0');
  const day = String(parts.day).padStart(2, '0');
  const hour = String(parts.hour).padStart(2, '0');
  const minute = String(parts.minute).padStart(2, '0');
  return `${year}-${month}-${day}T${hour}:${minute}`;
}

function getDatePartsInTimezone(date: Date, timezone: string): DateParts | null {
  try {
    const formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
    const parts = formatter.formatToParts(date);
    const map = new Map(parts.map(part => [part.type, part.value]));
    const year = Number(map.get('year'));
    const month = Number(map.get('month'));
    const day = Number(map.get('day'));
    const hour = Number(map.get('hour'));
    const minute = Number(map.get('minute'));
    const second = Number(map.get('second'));

    if ([year, month, day, hour, minute, second].some(value => Number.isNaN(value))) {
      return null;
    }

    return { year, month, day, hour, minute, second };
  } catch {
    return null;
  }
}

function toBrowserLocalDateTimeInput(isoDate: string): string {
  const date = new Date(isoDate);
  return formatDateTimeInput({
    year: date.getFullYear(),
    month: date.getMonth() + 1,
    day: date.getDate(),
    hour: date.getHours(),
    minute: date.getMinutes(),
    second: date.getSeconds(),
  });
}

function toTimezoneDateTimeInput(isoDate: string | null, timezone: string): string {
  if (!isoDate) return '';
  const date = new Date(isoDate);
  if (Number.isNaN(date.getTime())) return '';

  const parts = getDatePartsInTimezone(date, timezone);
  if (!parts) {
    return toBrowserLocalDateTimeInput(isoDate);
  }
  return formatDateTimeInput(parts);
}

function getTimezoneOffsetMs(date: Date, timezone: string): number | null {
  const parts = getDatePartsInTimezone(date, timezone);
  if (!parts) return null;

  const timezoneWallClockAsUtc = Date.UTC(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
    parts.second
  );
  return timezoneWallClockAsUtc - date.getTime();
}

function toUtcIso(localDateTime: string, timezone: string): string | null {
  if (!localDateTime) return null;
  const parsed = parseDateTimeInput(localDateTime);
  if (!parsed) return null;

  const wallClockAsUtc = Date.UTC(
    parsed.year,
    parsed.month - 1,
    parsed.day,
    parsed.hour,
    parsed.minute,
    0
  );
  let utcTimestamp = wallClockAsUtc;

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const offsetMs = getTimezoneOffsetMs(new Date(utcTimestamp), timezone);
    if (offsetMs === null) {
      const localDate = new Date(localDateTime);
      return Number.isNaN(localDate.getTime()) ? null : localDate.toISOString();
    }

    const adjustedUtcTimestamp = wallClockAsUtc - offsetMs;
    if (adjustedUtcTimestamp === utcTimestamp) {
      break;
    }
    utcTimestamp = adjustedUtcTimestamp;
  }

  return new Date(utcTimestamp).toISOString();
}

function defaultOneTimeInput(timezone: string): string {
  const date = new Date(Date.now() + 60 * 60 * 1000);
  return toTimezoneDateTimeInput(date.toISOString(), timezone);
}

function formatDateTime(isoDate: string | null, timezone: string): string {
  if (!isoDate) return 'Not set';
  const date = new Date(isoDate);
  if (Number.isNaN(date.getTime())) return 'Invalid date';
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: timezone,
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(date);
  }
}

function toDraft(task: ScheduleTask, timezone: string): ScheduleDraft {
  return {
    task_id: task.task_id,
    room_id: task.room_id,
    message: task.message,
    description: task.description,
    schedule_type: task.schedule_type,
    execute_at_input: toTimezoneDateTimeInput(task.execute_at, timezone),
    cron_expression: task.cron_expression ?? '',
  };
}

export function Schedules() {
  const { toast } = useToast();
  const [tasks, setTasks] = useState<ScheduleTask[]>([]);
  const [timezone, setTimezone] = useState('UTC');
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ScheduleDraft | null>(null);
  const [roomFilter, setRoomFilter] = useState<string>('all');
  const [isDirty, setIsDirty] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedTask = useMemo(
    () => tasks.find(task => task.task_id === selectedTaskId) ?? null,
    [tasks, selectedTaskId]
  );

  const visibleTasks = useMemo(
    () => (roomFilter === 'all' ? tasks : tasks.filter(task => task.room_id === roomFilter)),
    [tasks, roomFilter]
  );

  const roomOptions = useMemo(() => {
    const uniqueRooms = new Map<string, string>();
    tasks.forEach(task => {
      uniqueRooms.set(task.room_id, task.room_alias || task.room_id);
    });
    return Array.from(uniqueRooms.entries()).sort((a, b) => a[1].localeCompare(b[1]));
  }, [tasks]);

  const load = useCallback(async (showRefreshing = false) => {
    setError(null);
    if (showRefreshing) {
      setIsRefreshing(true);
    } else {
      setIsLoading(true);
    }

    try {
      const response = await listSchedules();
      setTasks(response.tasks);
      setTimezone(response.timezone);
      setSelectedTaskId(current =>
        current && response.tasks.some(task => task.task_id === current) ? current : null
      );
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Failed to load schedules';
      setError(message);
    } finally {
      setIsLoading(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setDraft(selectedTask ? toDraft(selectedTask, timezone) : null);
    setIsDirty(false);
  }, [selectedTask, timezone]);

  useEffect(() => {
    if (!selectedTaskId) return;
    if (roomFilter === 'all') return;
    if (!visibleTasks.some(task => task.task_id === selectedTaskId)) {
      setSelectedTaskId(null);
    }
  }, [roomFilter, selectedTaskId, visibleTasks]);

  useSwipeBack({
    onSwipeBack: () => setSelectedTaskId(null),
    enabled: !!selectedTaskId && window.innerWidth < 1024,
  });

  const scheduleItems: ScheduleListItem[] = useMemo(
    () =>
      visibleTasks.map(task => ({
        ...task,
        id: task.task_id,
        display_name: task.description || task.message,
      })),
    [visibleTasks]
  );

  const handleDraftChange = <K extends keyof ScheduleDraft>(field: K, value: ScheduleDraft[K]) => {
    if (!draft) return;
    setDraft({ ...draft, [field]: value });
    setIsDirty(true);
  };

  const handleScheduleTypeChange = (nextType: ScheduleType) => {
    if (!draft) return;
    setDraft({
      ...draft,
      schedule_type: nextType,
      execute_at_input:
        nextType === 'once'
          ? draft.execute_at_input || defaultOneTimeInput(timezone)
          : draft.execute_at_input,
    });
    setIsDirty(true);
  };

  const handleSave = async () => {
    if (!draft || !selectedTask) return;

    const message = draft.message.trim();
    const description = draft.description.trim();

    if (!message) {
      toast({
        title: 'Message required',
        description: 'The prompt message cannot be empty.',
        variant: 'destructive',
      });
      return;
    }

    const payload: UpdateScheduleRequest = {
      room_id: draft.room_id,
      message,
      description: description || message,
      schedule_type: draft.schedule_type,
    };

    if (draft.schedule_type === 'once') {
      const executeAtIso = toUtcIso(draft.execute_at_input, timezone);
      if (!executeAtIso) {
        toast({
          title: 'Time required',
          description: 'Set a valid date and time for one-time schedules.',
          variant: 'destructive',
        });
        return;
      }
      payload.execute_at = executeAtIso;
    } else {
      const cronExpression = draft.cron_expression.trim();
      if (!cronExpression) {
        toast({
          title: 'Cron required',
          description: 'Cron schedules require a cron expression.',
          variant: 'destructive',
        });
        return;
      }
      payload.cron_expression = cronExpression;
    }

    setIsSaving(true);
    try {
      await updateSchedule(selectedTask.task_id, payload);
      await load(true);
      toast({
        title: 'Schedule updated',
        description: `Task ${selectedTask.task_id} has been updated.`,
      });
      setIsDirty(false);
    } catch (e) {
      toast({
        title: 'Failed to update schedule',
        description: e instanceof Error ? e.message : 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setIsSaving(false);
    }
  };

  const handleCancelTask = async () => {
    if (!selectedTask) return;

    if (!confirm(`Cancel schedule ${selectedTask.task_id}?`)) {
      return;
    }

    setIsSaving(true);
    try {
      await cancelSchedule(selectedTask.task_id, selectedTask.room_id);
      setSelectedTaskId(null);
      await load(true);
      toast({
        title: 'Schedule cancelled',
        description: `Task ${selectedTask.task_id} was cancelled.`,
      });
    } catch (e) {
      toast({
        title: 'Failed to cancel schedule',
        description: e instanceof Error ? e.message : 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setIsSaving(false);
    }
  };

  const renderScheduleItem = (item: ScheduleListItem, isSelected: boolean) => {
    const nextRunLabel = item.next_run_at
      ? formatDateTime(item.next_run_at, timezone)
      : item.cron_description || 'Recurring';

    const badges: ItemCardBadge[] = [
      {
        content: item.schedule_type === 'once' ? 'One-time' : 'Cron',
        variant: item.schedule_type === 'once' ? 'secondary' : 'outline',
        icon: item.schedule_type === 'once' ? Timer : Repeat,
      },
      {
        content: item.room_alias || item.room_id,
        variant: 'outline',
        icon: MapPin,
      },
      {
        content: nextRunLabel,
        variant: 'secondary',
        icon: Clock3,
      },
    ];

    return (
      <ItemCard
        id={item.task_id}
        title={item.description || item.message}
        description={item.message}
        isSelected={isSelected}
        onClick={setSelectedTaskId}
        badges={badges}
      >
        <p className="mt-2 text-xs text-muted-foreground font-mono">{item.task_id}</p>
      </ItemCard>
    );
  };

  return (
    <div className="h-full flex flex-col gap-3 sm:gap-4">
      <Card className="border border-white/40 dark:border-white/10 bg-white/75 dark:bg-stone-900/50 backdrop-blur-xl">
        <CardContent className="py-3 px-4 sm:py-4 sm:px-5">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">{tasks.length} scheduled task(s)</Badge>
              <Badge variant="outline">Timezone: {timezone}</Badge>
              {roomFilter !== 'all' && (
                <Badge variant="outline">
                  Room: {roomOptions.find(([id]) => id === roomFilter)?.[1] || roomFilter}
                </Badge>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Select value={roomFilter} onValueChange={setRoomFilter}>
                <SelectTrigger className="w-[200px]">
                  <SelectValue placeholder="Filter by room" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All rooms</SelectItem>
                  {roomOptions.map(([id, label]) => (
                    <SelectItem key={id} value={id}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void load(true)}
                disabled={isRefreshing || isLoading}
              >
                {isRefreshing ? (
                  <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4 mr-1" />
                )}
                Refresh
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 sm:gap-4 flex-1 min-h-0">
        <div
          className={`col-span-1 lg:col-span-4 h-full overflow-hidden ${
            selectedTaskId ? 'hidden lg:block' : 'block'
          }`}
        >
          <ListPanel<ScheduleListItem>
            title="Schedules"
            icon={CalendarClock}
            items={scheduleItems}
            selectedId={selectedTaskId || undefined}
            onItemSelect={setSelectedTaskId}
            renderItem={renderScheduleItem}
            showSearch={true}
            searchPlaceholder="Search schedules..."
            showCreateButton={false}
            emptyIcon={CalendarClock}
            emptyMessage={isLoading ? 'Loading schedules...' : 'No schedules found'}
            emptySubtitle={
              isLoading
                ? 'Fetching scheduled tasks from Matrix state'
                : 'Create one with !schedule in Matrix'
            }
            searchFilter={(item, searchTerm) => {
              const term = searchTerm.toLowerCase();
              return (
                item.task_id.toLowerCase().includes(term) ||
                item.description.toLowerCase().includes(term) ||
                item.message.toLowerCase().includes(term) ||
                item.room_id.toLowerCase().includes(term) ||
                (item.room_alias || '').toLowerCase().includes(term)
              );
            }}
          />
        </div>

        <div
          className={`col-span-1 lg:col-span-8 h-full overflow-hidden ${
            selectedTaskId ? 'block' : 'hidden lg:block'
          }`}
        >
          {error ? (
            <EditorPanelEmptyState icon={CalendarClock} message={error} />
          ) : !selectedTask || !draft ? (
            <EditorPanelEmptyState icon={CalendarClock} message="Select a scheduled task to edit" />
          ) : (
            <EditorPanel
              icon={CalendarClock}
              title="Schedule Details"
              isDirty={isDirty}
              onSave={() => void handleSave()}
              onDelete={() => void handleCancelTask()}
              onBack={() => setSelectedTaskId(null)}
              disableSave={isSaving}
              disableDelete={isSaving}
            >
              <FieldGroup label="Task ID" helperText="Immutable schedule identifier">
                <Input value={draft.task_id} readOnly className="font-mono text-xs" />
              </FieldGroup>

              <FieldGroup label="Room" helperText="Where this scheduled task will execute">
                <Input
                  value={`${selectedTask.room_alias || selectedTask.room_id} (${
                    selectedTask.room_id
                  })`}
                  readOnly
                />
              </FieldGroup>

              <FieldGroup
                label="Prompt Message"
                helperText="This is the prompt posted automatically when the schedule runs."
                htmlFor="schedule-message"
              >
                <Textarea
                  id="schedule-message"
                  value={draft.message}
                  onChange={e => handleDraftChange('message', e.target.value)}
                  rows={5}
                  placeholder="@mindroom_agent your scheduled prompt..."
                />
              </FieldGroup>

              <FieldGroup
                label="Description"
                helperText="Human-readable description used in schedule lists."
                htmlFor="schedule-description"
              >
                <Input
                  id="schedule-description"
                  value={draft.description}
                  onChange={e => handleDraftChange('description', e.target.value)}
                />
              </FieldGroup>

              <FieldGroup
                label="Schedule Type"
                helperText="Choose one-time execution or recurring cron"
              >
                <Select
                  value={draft.schedule_type}
                  onValueChange={value => handleScheduleTypeChange(value as ScheduleType)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="once">One-time</SelectItem>
                    <SelectItem value="cron">Cron</SelectItem>
                  </SelectContent>
                </Select>
              </FieldGroup>

              {draft.schedule_type === 'once' ? (
                <FieldGroup
                  label="Run At"
                  helperText={`One-time execution timestamp (${timezone})`}
                  htmlFor="schedule-once-time"
                >
                  <Input
                    id="schedule-once-time"
                    type="datetime-local"
                    value={draft.execute_at_input}
                    onChange={e => handleDraftChange('execute_at_input', e.target.value)}
                  />
                </FieldGroup>
              ) : (
                <FieldGroup
                  label="Cron Expression"
                  helperText="Format: minute hour day month weekday (for example: 0 9 * * 1-5)"
                  htmlFor="schedule-cron"
                >
                  <Input
                    id="schedule-cron"
                    value={draft.cron_expression}
                    onChange={e => handleDraftChange('cron_expression', e.target.value)}
                    placeholder="0 9 * * *"
                  />
                </FieldGroup>
              )}

              <FieldGroup label="Next Run" helperText="Computed next execution time">
                <div className="rounded-md border px-3 py-2 text-sm text-muted-foreground flex items-center gap-2">
                  <Clock3 className="h-4 w-4" />
                  <span>{formatDateTime(selectedTask.next_run_at, timezone)}</span>
                </div>
              </FieldGroup>

              <FieldGroup label="Created" helperText="Creation metadata">
                <div className="space-y-1 text-sm text-muted-foreground">
                  <p>
                    {selectedTask.created_at
                      ? formatDateTime(selectedTask.created_at, timezone)
                      : 'Unknown'}
                  </p>
                  <p className="font-mono text-xs">
                    {selectedTask.created_by || 'Unknown creator'}
                  </p>
                  <p className="font-mono text-xs">{selectedTask.thread_id || 'No thread ID'}</p>
                </div>
              </FieldGroup>

              <div className="rounded-lg border border-amber-200/60 dark:border-amber-800/60 bg-amber-50/60 dark:bg-amber-900/15 p-3 text-sm text-amber-900 dark:text-amber-200 space-y-2">
                <div className="flex items-start gap-2">
                  <MessageSquare className="h-4 w-4 mt-0.5 shrink-0" />
                  <p>
                    Editing or cancelling here updates running schedules automatically (usually
                    within 30 seconds). You can also manage schedules in Matrix with
                    <span className="font-mono"> !list_schedules</span> and
                    <span className="font-mono"> !cancel_schedule {'<id>'}</span>.
                  </p>
                </div>
                <div className="flex items-start gap-2">
                  <CalendarClock className="h-4 w-4 mt-0.5 shrink-0" />
                  <p>
                    To create new schedules, use Matrix commands like
                    <span className="font-mono"> !schedule daily at 9am ...</span>.
                  </p>
                </div>
              </div>
            </EditorPanel>
          )}
        </div>
      </div>
    </div>
  );
}
