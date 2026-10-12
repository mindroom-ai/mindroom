import { ArrowLeft, Check, ChevronDown, Mic, Pause, Plus, SendHorizontal, Wrench } from 'lucide-react'
import { AgentAvatar, type AvatarName } from '@/components/landing/AgentAvatar'

const rooms: { name: string; topic: string; avatar: AvatarName; unread?: number; active?: boolean }[] = [
  { name: "Maya's Desk", topic: 'Private workspace with Mind', avatar: 'mind', active: true },
  { name: 'Launch War Room', topic: 'Q4 instrument launch', avatar: 'router', unread: 3 },
  { name: 'Home', topic: 'Lights, locks, and heating', avatar: 'home' },
  { name: 'Kitchen', topic: 'Dinners and groceries', avatar: 'general', unread: 1 },
]

const agents: { name: string; avatar: AvatarName; status: string; waiting?: boolean }[] = [
  { name: 'Mind', avatar: 'mind', status: 'Waiting for you', waiting: true },
  { name: 'Email', avatar: 'email', status: 'Drafting a reply' },
  { name: 'Research', avatar: 'research', status: 'Idle' },
]

const trace: { tool: string; done: boolean }[] = [
  { tool: 'search_email', done: true },
  { tool: 'check_calendar', done: true },
  { tool: 'create_event', done: false },
]

const panelLabelClass = 'text-xs font-semibold uppercase tracking-wide text-gray-600 dark:text-gray-400'

// A static picture of a MindRoom Chat room, drawn in HTML so it stays sharp and follows the theme.
export function ProductPreview() {
  return (
    <div
      role="img"
      aria-label="MindRoom Chat: Mind asks before booking a meeting, with the approval in the thread"
      className="overflow-hidden rounded-2xl bg-white text-left shadow-[0_24px_60px_-18px_rgba(47,33,97,0.28),0_6px_16px_-8px_rgba(47,33,97,0.18)] motion-safe:animate-rise dark:bg-gray-900 dark:shadow-none dark:outline dark:outline-1 dark:outline-white/10"
    >
      <div className="flex items-center gap-3 border-b border-gray-950/8 bg-gray-50/80 px-4 py-2.5 dark:border-white/8 dark:bg-white/[0.03]">
        <div className="flex gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full bg-gray-300 dark:bg-white/15" />
          <span className="h-2.5 w-2.5 rounded-full bg-gray-300 dark:bg-white/15" />
          <span className="h-2.5 w-2.5 rounded-full bg-gray-300 dark:bg-white/15" />
        </div>
        <div className="mx-auto rounded-md bg-gray-950/5 px-3 py-0.5 text-xs text-gray-600 dark:bg-white/8 dark:text-gray-400">
          chat.mindroom.chat
        </div>
        <div className="w-10" />
      </div>

      <div className="grid md:grid-cols-[200px_minmax(0,1fr)] lg:grid-cols-[220px_minmax(0,1fr)_230px]">
        <aside className="hidden border-r border-gray-950/8 bg-gray-50/60 p-3 dark:border-white/8 dark:bg-white/[0.02] md:block">
          <div className={`px-2 pb-2 pt-1 ${panelLabelClass}`}>Rooms</div>
          <ul className="space-y-0.5">
            {rooms.map((room) => (
              <li
                key={room.name}
                className={`flex items-center gap-2.5 rounded-lg px-2 py-2 ${room.active ? 'bg-brand-100 dark:bg-brand-400/12' : ''}`}
              >
                <AgentAvatar name={room.avatar} size={28} />
                <div className="min-w-0 flex-1">
                  <div className={`truncate text-[13px] font-semibold ${room.active ? 'text-brand-800 dark:text-brand-200' : 'text-gray-800 dark:text-gray-200'}`}>
                    {room.name}
                  </div>
                  <div className="truncate text-xs text-gray-600 dark:text-gray-400">{room.topic}</div>
                </div>
                {room.unread && (
                  <span className="rounded-full bg-brand-600 px-1.5 text-xs font-semibold leading-4 text-white dark:bg-brand-400 dark:text-brand-950">
                    {room.unread}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </aside>

        <section className="flex min-w-0 flex-col">
          <div className="flex items-center gap-3 border-b border-gray-950/8 px-4 py-3 dark:border-white/8">
            <AgentAvatar name="mind" size={32} />
            <div className="min-w-0">
              <div className="text-sm font-semibold text-gray-950 dark:text-white">Maya&apos;s Desk</div>
              <div className="truncate text-xs text-gray-500 dark:text-gray-400">Private workspace with Mind</div>
            </div>
          </div>
          <div className="mx-3 mt-3 flex items-center gap-2 rounded-lg border border-gray-950/8 px-3 py-2 text-xs dark:border-white/10">
            <ArrowLeft className="h-3.5 w-3.5 text-gray-500" />
            <span className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Thread view</span>
            <span className="truncate font-medium text-gray-700 dark:text-gray-200">Launch copy review with Grace</span>
          </div>

          <div className="flex-1 space-y-4 px-4 py-4 sm:px-5">
            <div className="flex gap-3">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-amber-200 to-rose-300 text-xs font-bold text-rose-950">
                MC
              </span>
              <div className="min-w-0">
                <div className="flex items-baseline gap-2">
                  <span className="text-[13px] font-semibold text-emerald-700 dark:text-emerald-400">Maya Chen</span>
                  <span className="text-xs text-gray-500 dark:text-gray-400">09:41</span>
                </div>
                <p className="mt-0.5 text-[13px] leading-5 text-gray-700 dark:text-gray-200">
                  <span className="rounded bg-brand-100 px-1 font-medium text-brand-800 dark:bg-brand-400/15 dark:text-brand-200">@Mind</span>{' '}
                  anything urgent? If Grace still needs time on the launch copy, book it.
                </p>
              </div>
            </div>

            <div className="flex gap-3">
              <AgentAvatar name="mind" size={32} />
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span className="text-[13px] font-semibold text-brand-700 dark:text-brand-300">Mind</span>
                  <span className="text-xs text-gray-500 dark:text-gray-400">09:41</span>
                </div>
                <div className="mt-1 inline-flex items-center gap-1.5 rounded-md border border-gray-950/10 bg-gray-50 px-2 py-1 text-xs font-medium text-gray-600 dark:border-white/10 dark:bg-white/5 dark:text-gray-300">
                  <Wrench className="h-3 w-3" />
                  2 tool calls
                  <ChevronDown className="h-3 w-3" />
                </div>
                <p className="mt-2 text-[13px] leading-5 text-gray-700 dark:text-gray-200">
                  Grace asked for 30 minutes on the launch copy before Friday. You are both free Thursday at 10:00.
                </p>
                <div className="mt-3 max-w-sm rounded-xl border border-brand-200 bg-brand-50/70 p-3 dark:border-brand-400/25 dark:bg-brand-400/8">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs font-medium text-brand-800 dark:text-brand-200">create_event</span>
                    <span className="inline-flex items-center gap-1 text-xs font-semibold text-amber-700 dark:text-amber-300">
                      <Pause className="h-3 w-3" />
                      Needs approval
                    </span>
                  </div>
                  <p className="mt-1.5 text-[13px] font-medium text-gray-900 dark:text-white">Launch copy review</p>
                  <p className="text-xs text-gray-600 dark:text-gray-400">Thu 10:00 to 10:30 · Maya, Grace</p>
                  <div className="mt-3 flex gap-2">
                    <span className="rounded-md bg-emerald-700 px-3 py-1 text-xs font-semibold text-white">Approve</span>
                    <span className="rounded-md border border-gray-950/10 bg-white px-3 py-1 text-xs font-semibold text-gray-700 dark:border-white/10 dark:bg-white/5 dark:text-gray-200">Deny</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div className="m-3 mt-0 flex items-center gap-2 rounded-xl border border-gray-950/10 px-3 py-2 text-gray-500 dark:border-white/10">
            <Plus className="h-4 w-4" />
            <span className="flex-1 text-[13px]">Send a message…</span>
            <Mic className="h-4 w-4" />
            <SendHorizontal className="h-4 w-4 text-brand-600 dark:text-brand-400" />
          </div>
        </section>

        <aside className="hidden border-l border-gray-950/8 p-4 dark:border-white/8 lg:block">
          <div className={panelLabelClass}>Agents in this room</div>
          <ul className="mt-3 space-y-2.5">
            {agents.map((agent) => (
              <li key={agent.name} className="flex items-center gap-2.5">
                <AgentAvatar name={agent.avatar} size={28} />
                <div className="min-w-0">
                  <div className="text-[13px] font-semibold text-gray-900 dark:text-white">{agent.name}</div>
                  <div className={`flex items-center gap-1.5 text-xs ${agent.waiting ? 'text-amber-700 dark:text-amber-300' : 'text-gray-600 dark:text-gray-400'}`}>
                    <span className={`h-1.5 w-1.5 rounded-full ${agent.waiting ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`} />
                    {agent.status}
                  </div>
                </div>
              </li>
            ))}
          </ul>
          <div className={`mt-6 ${panelLabelClass}`}>Run trace</div>
          <ol className="mt-3 space-y-2">
            {trace.map((step) => (
              <li key={step.tool} className="flex items-center gap-2 font-mono text-xs text-gray-600 dark:text-gray-300">
                {step.done ? (
                  <Check className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
                ) : (
                  <Pause className="h-3.5 w-3.5 text-amber-600 dark:text-amber-300" />
                )}
                {step.tool}
              </li>
            ))}
          </ol>
        </aside>
      </div>
    </div>
  )
}
