'use client'

import { Lock, Users } from 'lucide-react'
import { useEffect, useState } from 'react'

function Chip({ label, className = '' }: { label: string; className?: string }) {
  return (
    <span className={`px-2 py-0.5 text-xs rounded-full bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-600 ${className}`}>
      {label}
    </span>
  )
}

function Avatar({ name, color }: { name: string; color: string }) {
  const initials = name
    .replace(/[@]/g, '')
    .split(/\s|_/)
    .map((p) => p[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
  return (
    <div className={`w-6 h-6 md:w-8 md:h-8 rounded-full flex items-center justify-center text-white text-[10px] md:text-xs font-bold ${color}`}>{initials}</div>
  )
}

function ChatBubble({
  side,
  name,
  org,
  text,
  chips = [],
  color,
  isAgent = false,
  orgDomain = '',
}: {
  side: 'left' | 'right'
  name: string
  org: string
  text: React.ReactNode
  chips?: string[]
  color: string
  isAgent?: boolean
  orgDomain?: string
}) {
  // Extract domain for visual differentiation
  const domainColor = orgDomain?.includes('org-a') ? 'blue' : orgDomain?.includes('org-b') ? 'green' : 'gray'
  const borderAccent =
    isAgent ? 'border-l-4 border-orange-400/60' :
    domainColor === 'blue' ? 'border-l-4 border-blue-400/60' :
    domainColor === 'green' ? 'border-l-4 border-green-400/60' :
    ''

  return (
    <div className={`flex ${side === 'right' ? 'justify-end' : 'justify-start'}`}>
      {side === 'left' && <Avatar name={name} color={color} />}
      <div className={`mx-2 max-w-[90%] ${side === 'right' ? 'items-end text-right' : ''}`}>
        <div className={`flex items-center gap-2 mb-1 ${side === 'right' ? 'justify-end' : ''}`}>
          <span className="text-xs md:text-sm font-semibold text-gray-700 dark:text-gray-200">
            {name}
            {!isAgent && orgDomain && (
              <span className="font-normal text-gray-500">:{orgDomain}</span>
            )}
          </span>
          {isAgent && (
            <span className="text-[10px] px-1.5 py-0.5 bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-400 rounded">
              AI Agent
            </span>
          )}
          {chips.map((c, i) => (
            <Chip key={i} label={c} className="hidden md:inline-flex" />
          ))}
        </div>
        <div
          className={`rounded-2xl px-3 py-2 text-sm border ${borderAccent} ${
            side === 'right'
              ? 'bg-gray-50 dark:bg-gray-700 text-gray-900 dark:text-gray-100 border-gray-200 dark:border-gray-600'
              : 'bg-white/90 dark:bg-gray-800/80 text-gray-800 dark:text-gray-200 border-gray-200 dark:border-gray-700'
          }`}
        >
          {text}
        </div>
      </div>
      {side === 'right' && <Avatar name={name} color={color} />}
    </div>
  )
}

export function Collaboration() {
  const [isVisible, setIsVisible] = useState(false)
  const [tab, setTab] = useState<'business' | 'personal'>('business')
  useEffect(() => {
    const obs = new IntersectionObserver(
      ([entry]) => entry.isIntersecting && setIsVisible(true),
      { threshold: 0.1 }
    )
    const el = document.getElementById('collaboration')
    if (el) obs.observe(el)
    return () => el && obs.unobserve(el)
  }, [])

  return (
    <section id="collaboration" className="py-16 md:py-20 px-6 bg-gradient-to-b from-white to-gray-50 dark:from-gray-900 dark:to-gray-800">
      <div className="container mx-auto max-w-6xl">
        <div className="text-center mb-12">
          <h2 className="text-3xl md:text-4xl font-bold bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Collaboration Scenarios
          </h2>
          <p className="text-gray-600 dark:text-gray-300">Two familiar examples: Business and Personal</p>
        </div>
        {/* Tabs */}
        <div className="flex items-center justify-center gap-2 md:gap-3 mb-4 md:mb-6">
          <button
            onClick={() => setTab('business')}
            className={`px-3 py-1.5 md:px-4 md:py-2 rounded-full text-sm font-medium border transition ${
              tab === 'business'
                ? 'bg-orange-500 text-white border-orange-600 shadow'
                : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-700 hover:border-orange-300 dark:hover:border-orange-700'
            }`}
          >
            Business
          </button>
          <button
            onClick={() => setTab('personal')}
            className={`px-3 py-1.5 md:px-4 md:py-2 rounded-full text-sm font-medium border transition ${
              tab === 'personal'
                ? 'bg-orange-500 text-white border-orange-600 shadow'
                : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-700 hover:border-orange-300 dark:hover:border-orange-700'
            }`}
          >
            Personal
          </button>
        </div>

        {/* Chat mock */}
        <div className={`mb-6 md:mb-8 ${isVisible ? 'fade-in-up' : 'opacity-0'}`}>
          <div className="rounded-2xl border border-gray-200 dark:border-gray-700 bg-white/80 dark:bg-gray-800/80 backdrop-blur shadow-lg overflow-hidden">
            <div className="px-3 md:px-4 py-2.5 md:py-3 flex items-center justify-between border-b border-gray-200 dark:border-gray-700">
              <div className="flex items-center gap-2 md:gap-3">
                <Users className="w-4 h-4 text-gray-500" />
                {tab === 'business' ? (
                  <div>
                    <div className="text-sm font-semibold text-gray-800 dark:text-gray-200">#q4‑planning</div>
                    <div className="text-[11px] md:text-xs text-gray-500">
                      Federated room · org‑a.com ⇄ org‑b.net
                    </div>
                  </div>
                ) : (
                  <div>
                    <div className="text-sm font-semibold text-gray-800 dark:text-gray-200">#weekend‑hike</div>
                    <div className="text-[11px] md:text-xs text-gray-500">Encrypted room · friends</div>
                  </div>
                )}
              </div>
              <div className="flex items-center gap-2">
                {/* Mobile: show compact participant summary */}
                <span className="text-xs text-gray-600 dark:text-gray-300 md:hidden">
                  {tab === 'business' ? '4 participants' : '6 participants'}
                </span>
                {/* Desktop: show participant chips */}
                <div className="hidden md:flex items-center gap-2 text-xs">
                  {tab === 'business' ? (
                    <>
                      <Chip label="alice (org‑a)" />
                      <Chip label="@mindroom_analyst" />
                      <Chip label="bob (org‑b)" />
                      <Chip label="@client_architect" />
                    </>
                  ) : (
                    <>
                      <Chip label="alice" />
                      <Chip label="bob" />
                      <Chip label="carol" />
                      <Chip label="@alice_calendar" />
                      <Chip label="@bob_calendar" />
                      <Chip label="@carol_calendar" />
                    </>
                  )}
                </div>
                <Lock className="w-4 h-4 text-green-600" />
              </div>
            </div>

            <div className="p-3 md:p-4 space-y-2 md:space-y-3">
              {tab === 'business' ? (
                <>
                  <ChatBubble
                    side="left"
                    name="alice"
                    org="Matrix · org‑a.com"
                    orgDomain="org-a.com"
                    color="bg-indigo-500"
                    text={<>@mindroom_analyst pull Q4 conversion vs target and propose actions</>}
                  />
                  <ChatBubble
                    side="right"
                    name="@mindroom_analyst"
                    org="Matrix · agent"
                    isAgent={true}
                    color="bg-orange-500"
                    chips={["DB", "Analytics"]}
                    text={<>Fetching from DB + analytics… Chart attached. We're 13% below target on paid; suggest realloc + SEO refresh.</>}
                  />
                  <ChatBubble
                    side="left"
                    name="bob"
                    org="Matrix · org‑b.net"
                    orgDomain="org-b.net"
                    color="bg-emerald-600"
                    text={<>@client_architect is this compatible with our data model?</>}
                  />
                  <ChatBubble
                    side="right"
                    name="@client_architect"
                    org="Matrix · agent"
                    isAgent={true}
                    color="bg-sky-600"
                    text={<>Yes, schema v2 OK; can push PR to your repo when you approve.</>}
                  />
                  <ChatBubble
                    side="left"
                    name="alice"
                    org="Matrix · org‑a.com"
                    orgDomain="org-a.com"
                    color="bg-indigo-500"
                    text={<>Approved. @mindroom_analyst sync brief to Slack #marketing (via bridge).</>}
                  />
                  <ChatBubble
                    side="right"
                    name="@mindroom_analyst"
                    org="Matrix · agent"
                    isAgent={true}
                    color="bg-orange-500"
                    chips={["Slack bridge"]}
                    text={<>Posted in Slack and invited @client_architect (read‑only).</>}
                  />
                </>
              ) : (
                <>
                  <ChatBubble
                    side="left"
                    name="alice"
                    org="Matrix"
                    color="bg-indigo-500"
                    text={<>Can we pick a weekend for the hike?</>}
                  />
                  <ChatBubble
                    side="right"
                    name="@alice_calendar"
                    org="Matrix · agent"
                    isAgent={true}
                    color="bg-orange-500"
                    chips={["Calendar"]}
                    text={<>Checking weekends for Alice…</>}
                  />
                  <ChatBubble
                    side="right"
                    name="@bob_calendar"
                    org="Matrix · agent"
                    isAgent={true}
                    color="bg-sky-600"
                    chips={["Calendar"]}
                    text={<>Bob is free Sat 14:00–18:00; busy Sunday morning.</>}
                  />
                  <ChatBubble
                    side="right"
                    name="@carol_calendar"
                    org="Matrix · agent"
                    isAgent={true}
                    color="bg-emerald-600"
                    chips={["Calendar"]}
                    text={<>Carol is free Sunday 10:00–13:00; Sat is open after 17:00.</>}
                  />
                  <ChatBubble
                    side="left"
                    name="bob"
                    org="Matrix"
                    color="bg-emerald-600"
                    text={<>Let's do Sunday 11:00 at the trailhead.</>}
                  />
                  <ChatBubble
                    side="right"
                    name="@alice_calendar"
                    org="Matrix · agent"
                    isAgent={true}
                    color="bg-orange-500"
                    chips={["Invites", "Discord bridge"]}
                    text={<>Invites sent and summary posted to Discord #friends (via bridge).</>}
                  />
                </>
              )}
            </div>
          </div>
        </div>
        {/* Simple federation/bridge callout */}
        <div className="text-center mt-2 text-[13px] md:text-sm text-gray-600 dark:text-gray-300 max-w-3xl mx-auto">
          <p>
            <strong>True Federation:</strong> Different organizations (org-a.com, org-b.net) collaborate in one encrypted room.
            Each participant is a real, verifiable Matrix account on their own server.
          </p>
          <p className="mt-1">
            <strong>Bridges:</strong> Connect existing tools (Slack, Discord, Telegram) to Matrix rooms. Bridge connections are not end-to-end encrypted.
          </p>
        </div>
      </div>
    </section>
  )
}
