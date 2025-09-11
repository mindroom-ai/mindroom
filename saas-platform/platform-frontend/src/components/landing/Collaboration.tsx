'use client'

import { Lock, Users, Share2 } from 'lucide-react'
import { useEffect, useState } from 'react'

function Chip({ label }: { label: string }) {
  return (
    <span className="px-2 py-0.5 text-xs rounded-full bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-600">
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
    <div className={`w-7 h-7 rounded-full flex items-center justify-center text-white text-xs font-bold ${color}`}>{initials}</div>
  )
}

function ChatBubble({
  side,
  name,
  org,
  text,
  chips = [],
  color,
}: {
  side: 'left' | 'right'
  name: string
  org: string
  text: React.ReactNode
  chips?: string[]
  color: string
}) {
  return (
    <div className={`flex ${side === 'right' ? 'justify-end' : 'justify-start'}`}>
      {side === 'left' && <Avatar name={name} color={color} />}
      <div className={`mx-2 max-w-[90%]`}>
        <div className="flex items-center gap-2 mb-1">
          <span className="text-xs font-semibold text-gray-700 dark:text-gray-200">{name}</span>
          <span className="text-[10px] text-gray-500">{org}</span>
          {chips.map((c, i) => (
            <Chip key={i} label={c} />
          ))}
        </div>
        <div
          className={`rounded-2xl px-3 py-2 text-sm border ${
            side === 'right'
              ? 'bg-orange-50 dark:bg-orange-900/20 text-gray-900 dark:text-gray-100 border-orange-200 dark:border-orange-800'
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
    <section id="collaboration" className="py-20 px-6 bg-gradient-to-b from-white to-gray-50 dark:from-gray-900 dark:to-gray-800">
      <div className="container mx-auto max-w-6xl">
        <div className="text-center mb-12">
          <h2 className="text-3xl md:text-4xl font-bold bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Collaboration In Action
          </h2>
          <p className="text-gray-600 dark:text-gray-300">A familiar chat experience and clear federation model</p>
        </div>

        {/* Chat mock */}
        <div className={`mb-10 ${isVisible ? 'fade-in-up' : 'opacity-0'}`}>
          <div className="rounded-2xl border border-gray-200 dark:border-gray-700 bg-white/80 dark:bg-gray-800/80 backdrop-blur shadow-lg overflow-hidden">
            <div className="px-4 py-3 flex items-center justify-between border-b border-gray-200 dark:border-gray-700">
              <div className="flex items-center gap-3">
                <Users className="w-4 h-4 text-gray-500" />
                <div>
                  <div className="text-sm font-semibold text-gray-800 dark:text-gray-200">#q4‑planning</div>
                  <div className="text-xs text-gray-500">Encrypted room · org‑a.com</div>
                </div>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <Chip label="alice (org‑a)" />
                <Chip label="@mindroom_analyst" />
                <Chip label="bob (org‑b)" />
                <Chip label="@client_architect" />
                <Lock className="w-4 h-4 text-green-600" />
              </div>
            </div>

            <div className="p-4 space-y-3">
              <ChatBubble
                side="left"
                name="alice"
                org="Matrix · org‑a.com"
                color="bg-indigo-500"
                text={
                  <>
                    <span className="text-gray-800 dark:text-gray-200">@mindroom_analyst pull Q4 conversion vs target and propose actions</span>
                  </>
                }
              />

              <ChatBubble
                side="right"
                name="@mindroom_analyst"
                org="Matrix · agent"
                color="bg-orange-500"
                chips={["DB", "Analytics"]}
                text={
                  <>
                    Fetching from DB + analytics… Chart attached. We’re 13% below target on paid; suggest realloc + SEO refresh.
                  </>
                }
              />

              <ChatBubble
                side="left"
                name="bob"
                org="Matrix · org‑b.net"
                color="bg-emerald-600"
                text={
                  <>
                    <span className="text-gray-800 dark:text-gray-200">@client_architect is this compatible with our data model?</span>
                  </>
                }
              />

              <ChatBubble
                side="right"
                name="@client_architect"
                org="Matrix · agent"
                color="bg-sky-600"
                text={
                  <>Yes, schema v2 OK; can push PR to your repo when you approve.</>
                }
              />

              <ChatBubble
                side="left"
                name="alice"
                org="Matrix · org‑a.com"
                color="bg-indigo-500"
                text={
                  <>Approved. @mindroom_analyst sync brief to Slack #marketing (via bridge).</>
                }
              />

              <ChatBubble
                side="right"
                name="@mindroom_analyst"
                org="Matrix · agent"
                color="bg-orange-500"
                chips={["Slack bridge"]}
                text={<>Posted in Slack and invited @client_architect (read‑only).</>}
              />
            </div>
          </div>
        </div>

        {/* Federation diagrams */}
        <div className="grid md:grid-cols-2 gap-6">
          {/* Minimalist */}
          <div className={`rounded-2xl border border-gray-200 dark:border-gray-700 bg-white/80 dark:bg-gray-800/80 p-6 ${isVisible ? 'fade-in-up' : 'opacity-0'}`}>
            <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-gray-800 dark:text-gray-200">
              <Share2 className="w-4 h-4" /> Minimalist Federation Diagram
            </div>
            <div className="relative">
              <div className="grid grid-cols-3 items-center">
                <div className="space-y-2">
                  <div className="p-3 rounded-xl border border-gray-200 dark:border-gray-700">
                    <div className="text-xs text-gray-500">Org A</div>
                    <div className="font-semibold text-sm">org‑a.com</div>
                    <div className="mt-2 flex flex-wrap gap-1 text-[10px] text-gray-500">
                      <Chip label="alice" />
                      <Chip label="@mindroom_analyst" />
                    </div>
                  </div>
                </div>
                <div className="flex flex-col items-center">
                  <div className="px-4 py-2 rounded-full bg-gray-100 dark:bg-gray-700 text-xs text-gray-700 dark:text-gray-200 border border-gray-200 dark:border-gray-600 flex items-center gap-2">
                    <Lock className="w-4 h-4 text-green-600" /> Encrypted Room
                  </div>
                  <div className="h-10 w-0.5 bg-gradient-to-b from-gray-300 to-gray-400 dark:from-gray-600 dark:to-gray-500 my-2" />
                  <div className="flex flex-wrap gap-2 justify-center text-[10px]">
                    <Chip label="Slack (bridge)" />
                    <Chip label="Discord (bridge)" />
                    <Chip label="Telegram (bridge)" />
                  </div>
                </div>
                <div className="space-y-2">
                  <div className="p-3 rounded-xl border border-gray-200 dark:border-gray-700">
                    <div className="text-xs text-gray-500">Org B</div>
                    <div className="font-semibold text-sm">org‑b.net</div>
                    <div className="mt-2 flex flex-wrap gap-1 text-[10px] text-gray-500">
                      <Chip label="bob" />
                      <Chip label="@client_architect" />
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <ul className="mt-4 text-sm text-gray-700 dark:text-gray-300 space-y-1 list-disc pl-5">
              <li>Cross‑org, single thread via Matrix federation</li>
              <li>Consistent identity (humans + agents as real accounts)</li>
              <li>Bridges where people work (Slack/Discord/Telegram)</li>
            </ul>
          </div>

          {/* Playful */}
          <div className={`rounded-2xl border border-gray-200 dark:border-gray-700 bg-white/80 dark:bg-gray-800/80 p-6 ${isVisible ? 'fade-in-up' : 'opacity-0'}`}>
            <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-gray-800 dark:text-gray-200">
              <Share2 className="w-4 h-4" /> Playful Federation Diagram
            </div>
            <div className="relative flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Avatar name="alice" color="bg-indigo-500" />
                <Avatar name="@mindroom_analyst" color="bg-orange-500" />
                <span className="text-xs text-gray-500">org‑a.com</span>
              </div>
              <div className="flex items-center gap-2">
                <div className="px-3 py-1 rounded-full bg-gray-100 dark:bg-gray-700 text-xs text-gray-700 dark:text-gray-200 border border-gray-200 dark:border-gray-600 flex items-center gap-2">
                  <Lock className="w-4 h-4 text-green-600" /> Room
                </div>
                <div className="flex flex-wrap gap-1 ml-2">
                  <Chip label="Slack" />
                  <Chip label="Discord" />
                  <Chip label="Telegram" />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Avatar name="bob" color="bg-emerald-600" />
                <Avatar name="@client_architect" color="bg-sky-600" />
                <span className="text-xs text-gray-500">org‑b.net</span>
              </div>
            </div>
            <p className="mt-4 text-sm text-gray-700 dark:text-gray-300">
              Two organizations share one encrypted conversation. Agents and humans collaborate as peers; outcomes are bridged to the tools your teams already use.
            </p>
          </div>
        </div>
      </div>
    </section>
  )
}
