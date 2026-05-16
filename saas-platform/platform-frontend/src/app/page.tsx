'use client'

import Link from 'next/link'
import { DarkModeToggle } from '@/components/DarkModeToggle'
import { MindRoomLogo } from '@/components/MindRoomLogo'
import {
  ArrowRight,
  Bot,
  Check,
  Code2,
  GitBranch,
  Github,
  KeyRound,
  Lock,
  MessageSquare,
  Network,
  Shield,
  Sparkles,
  TerminalSquare,
  Workflow,
  type LucideIcon,
} from 'lucide-react'

type IconItem = {
  title: string
  body: string
  icon: LucideIcon
}

type PricePlan = {
  name: string
  price: string
  description: string
  features: string[]
  cta: string
  href: string
  highlighted?: boolean
}

const navLinks = [
  { href: '#workflow', label: 'Workflow' },
  { href: '#platform', label: 'Platform' },
  { href: '#security', label: 'Security' },
  { href: '#pricing', label: 'Pricing' },
]

const workflow: IconItem[] = [
  {
    title: 'Create rooms for real work',
    body: 'Give each room a purpose, invite people, and bring in the agents that should participate.',
    icon: MessageSquare,
  },
  {
    title: 'Route work to specialists',
    body: 'Agents can answer directly, coordinate as a team, or stay quiet until they are mentioned.',
    icon: Bot,
  },
  {
    title: 'Keep the audit trail',
    body: 'Threads, tool traces, schedules, and decisions remain in the room where the work happened.',
    icon: Workflow,
  },
]

const platform: IconItem[] = [
  {
    title: 'Matrix-native runtime',
    body: 'MindRoom runs on Matrix so rooms, bridges, identity, and federation are part of the product model.',
    icon: Network,
  },
  {
    title: 'Code and tool execution',
    body: 'Agents can use shell, file, browser, calendar, GitHub, and custom tools with policy controls.',
    icon: TerminalSquare,
  },
  {
    title: 'Hosted or self-managed',
    body: 'Use the hosted SaaS control plane, deploy instances on Kubernetes, or run the open-source stack yourself.',
    icon: GitBranch,
  },
  {
    title: 'Model-provider flexible',
    body: 'Configure OpenAI, Anthropic, Google, OpenRouter, local OpenAI-compatible servers, and per-agent defaults.',
    icon: Sparkles,
  },
]

const security: IconItem[] = [
  {
    title: 'Private-by-default rooms',
    body: 'Hosted instances can start with owner-scoped authorization and invite-only room behavior.',
    icon: Lock,
  },
  {
    title: 'Credential isolation',
    body: 'Provider keys and tool credentials are stored separately from public configuration and runtime code.',
    icon: KeyRound,
  },
  {
    title: 'Operational boundaries',
    body: 'Kubernetes workers, network policies, rate limits, and audit logs keep hosted deployments inspectable.',
    icon: Shield,
  },
]

const plans: PricePlan[] = [
  {
    name: 'Free',
    price: '$0',
    description: 'Try one agent in a hosted room.',
    features: ['1 agent', '100 messages per day', 'Community support'],
    cta: 'Start free',
    href: '/auth/signup',
  },
  {
    name: 'Starter',
    price: '$10',
    description: 'For personal projects and small workflows.',
    features: ['More agents', 'Hosted instance', 'All integrations'],
    cta: 'Create account',
    href: '/auth/signup',
    highlighted: true,
  },
  {
    name: 'Teams',
    price: '$8 / user',
    description: 'For shared workspaces and agent teams.',
    features: ['Unlimited agents', 'SSO-ready auth', 'Priority support'],
    cta: 'Open dashboard',
    href: '/dashboard',
  },
]

function SectionHeading({
  eyebrow,
  title,
  body,
}: {
  eyebrow: string
  title: string
  body: string
}) {
  return (
    <div className="max-w-2xl">
      <p className="text-sm font-semibold uppercase tracking-normal text-orange-600 dark:text-orange-400">
        {eyebrow}
      </p>
      <h2 className="mt-3 text-3xl font-semibold text-gray-950 dark:text-white sm:text-4xl">
        {title}
      </h2>
      <p className="mt-4 text-base leading-7 text-gray-600 dark:text-gray-300">
        {body}
      </p>
    </div>
  )
}

function ProductPreview() {
  const messages = [
    {
      actor: 'router',
      body: 'This looks like deployment work. I can route it to infra and security.',
    },
    {
      actor: 'infra',
      body: 'Provisioning a NixOS host, wiring DNS, and checking service health.',
    },
    {
      actor: 'security',
      body: 'Reviewing redirects, auth callbacks, headers, and secret exposure.',
    },
  ]

  return (
    <div className="relative mx-auto max-w-5xl overflow-hidden rounded-lg border border-gray-200 bg-white shadow-2xl shadow-gray-200/70 dark:border-gray-800 dark:bg-gray-950 dark:shadow-black/30">
      <div className="flex items-center justify-between border-b border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900">
        <div className="flex items-center gap-2">
          <span className="h-3 w-3 rounded-full bg-red-400" />
          <span className="h-3 w-3 rounded-full bg-yellow-400" />
          <span className="h-3 w-3 rounded-full bg-green-400" />
        </div>
        <div className="text-xs font-medium text-gray-500 dark:text-gray-400">
          #deployment-room
        </div>
      </div>
      <div className="grid min-h-[360px] md:grid-cols-[220px_1fr_260px]">
        <aside className="hidden border-r border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900 md:block">
          <div className="text-xs font-semibold uppercase tracking-normal text-gray-500 dark:text-gray-500">
            Rooms
          </div>
          {['Lobby', 'Deployment', 'Research', 'Support'].map((room, index) => (
            <div
              key={room}
              className={`mt-3 rounded-md px-3 py-2 text-sm ${
                index === 1
                  ? 'bg-white font-medium text-gray-950 shadow-sm dark:bg-gray-800 dark:text-white'
                  : 'text-gray-600 dark:text-gray-400'
              }`}
            >
              {room}
            </div>
          ))}
        </aside>
        <section className="p-4 sm:p-6">
          <div className="mb-5 flex flex-wrap items-center gap-2">
            {['infra', 'security', 'docs'].map((agent) => (
              <span
                key={agent}
                className="rounded-md border border-gray-200 px-2.5 py-1 text-xs font-medium text-gray-600 dark:border-gray-800 dark:text-gray-300"
              >
                @{agent}
              </span>
            ))}
          </div>
          <div className="space-y-4">
            {messages.map((message) => (
              <div key={message.actor} className="rounded-lg border border-gray-200 p-4 dark:border-gray-800">
                <div className="mb-2 flex items-center gap-2">
                  <div className="flex h-7 w-7 items-center justify-center rounded-md bg-orange-100 text-orange-700 dark:bg-orange-500/15 dark:text-orange-300">
                    <Bot className="h-4 w-4" />
                  </div>
                  <div className="text-sm font-semibold text-gray-950 dark:text-white">@{message.actor}</div>
                </div>
                <p className="text-sm leading-6 text-gray-600 dark:text-gray-300">{message.body}</p>
              </div>
            ))}
          </div>
        </section>
        <aside className="hidden border-l border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900 md:block">
          <div className="text-xs font-semibold uppercase tracking-normal text-gray-500 dark:text-gray-500">
            Run trace
          </div>
          {['DNS checked', 'OAuth callback tested', 'Secrets scan clean'].map((item) => (
            <div key={item} className="mt-4 flex items-start gap-2 text-sm text-gray-600 dark:text-gray-300">
              <Check className="mt-0.5 h-4 w-4 text-emerald-600 dark:text-emerald-400" />
              <span>{item}</span>
            </div>
          ))}
        </aside>
      </div>
    </div>
  )
}

function IconGrid({ items }: { items: IconItem[] }) {
  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
      {items.map((item) => {
        const Icon = item.icon
        return (
          <article key={item.title} className="rounded-lg border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-gray-950">
            <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-md bg-gray-100 text-gray-900 dark:bg-gray-900 dark:text-white">
              <Icon className="h-5 w-5" />
            </div>
            <h3 className="text-base font-semibold text-gray-950 dark:text-white">{item.title}</h3>
            <p className="mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">{item.body}</p>
          </article>
        )
      })}
    </div>
  )
}

export default function LandingPage() {
  return (
    <main className="min-h-screen bg-white text-gray-950 dark:bg-gray-950 dark:text-white">
      <nav className="sticky top-0 z-50 border-b border-gray-200 bg-white/95 backdrop-blur dark:border-gray-800 dark:bg-gray-950/95">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-4 sm:px-6 lg:px-8">
          <Link href="/" className="flex items-center gap-3" aria-label="MindRoom home">
            <MindRoomLogo className="text-orange-600 dark:text-orange-400" size={32} />
            <span className="text-lg font-semibold">MindRoom</span>
          </Link>
          <div className="hidden items-center gap-7 lg:flex">
            {navLinks.map((link) => (
              <Link key={link.href} href={link.href} className="text-sm font-medium text-gray-600 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white">
                {link.label}
              </Link>
            ))}
          </div>
          <div className="flex items-center gap-2 sm:gap-3">
            <DarkModeToggle />
            <Link href="/auth/login" className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-950 dark:text-gray-300 dark:hover:bg-gray-900 dark:hover:text-white sm:inline-flex">
              Sign in
            </Link>
            <Link href="/auth/signup" className="inline-flex items-center gap-2 rounded-md bg-gray-950 px-4 py-2 text-sm font-semibold text-white hover:bg-gray-800 dark:bg-white dark:text-gray-950 dark:hover:bg-gray-200">
              Start free
              <ArrowRight className="h-4 w-4" />
            </Link>
          </div>
        </div>
      </nav>

      <section className="relative overflow-hidden border-b border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-950">
        <div className="mx-auto max-w-7xl px-4 pb-12 pt-16 sm:px-6 sm:pt-20 lg:px-8">
          <div className="max-w-3xl">
            <p className="inline-flex rounded-md border border-orange-200 bg-orange-50 px-3 py-1 text-sm font-medium text-orange-700 dark:border-orange-500/25 dark:bg-orange-500/10 dark:text-orange-300">
              Matrix-native AI agents for real team rooms
            </p>
            <h1 className="mt-6 text-5xl font-semibold text-gray-950 dark:text-white sm:text-6xl lg:text-7xl">
              MindRoom
            </h1>
            <p className="mt-6 max-w-2xl text-lg leading-8 text-gray-600 dark:text-gray-300">
              Deploy AI agents where work already happens: in chat rooms, threads, bridges, and shared operational workflows.
              MindRoom gives each agent a role, a memory, tools, and a place to collaborate with people.
            </p>
            <div className="mt-8 flex flex-col gap-3 sm:flex-row">
              <Link href="/auth/signup" className="inline-flex items-center justify-center gap-2 rounded-md bg-gray-950 px-5 py-3 text-sm font-semibold text-white hover:bg-gray-800 dark:bg-white dark:text-gray-950 dark:hover:bg-gray-200">
                Create hosted workspace
                <ArrowRight className="h-4 w-4" />
              </Link>
              <a href="https://github.com/mindroom-ai/mindroom" target="_blank" rel="noopener noreferrer" className="inline-flex items-center justify-center gap-2 rounded-md border border-gray-300 bg-white px-5 py-3 text-sm font-semibold text-gray-800 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 dark:hover:bg-gray-900">
                <Github className="h-4 w-4" />
                View source
              </a>
            </div>
          </div>
          <div className="mt-12">
            <ProductPreview />
          </div>
        </div>
      </section>

      <section id="workflow" className="border-b border-gray-200 py-20 dark:border-gray-800">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <SectionHeading
            eyebrow="Workflow"
            title="Rooms become durable workspaces."
            body="MindRoom is not another isolated chat window. It keeps agents, people, decisions, and tool output in the same conversational surface."
          />
          <div className="mt-10 grid gap-4 md:grid-cols-3">
            {workflow.map((item, index) => {
              const Icon = item.icon
              return (
                <article key={item.title} className="rounded-lg border border-gray-200 p-6 dark:border-gray-800">
                  <div className="mb-5 flex items-center justify-between">
                    <div className="flex h-11 w-11 items-center justify-center rounded-md bg-orange-100 text-orange-700 dark:bg-orange-500/15 dark:text-orange-300">
                      <Icon className="h-5 w-5" />
                    </div>
                    <span className="text-sm font-semibold text-gray-400">0{index + 1}</span>
                  </div>
                  <h3 className="text-lg font-semibold text-gray-950 dark:text-white">{item.title}</h3>
                  <p className="mt-3 text-sm leading-6 text-gray-600 dark:text-gray-300">{item.body}</p>
                </article>
              )
            })}
          </div>
        </div>
      </section>

      <section id="platform" className="border-b border-gray-200 bg-gray-50 py-20 dark:border-gray-800 dark:bg-gray-900/30">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <SectionHeading
            eyebrow="Platform"
            title="Built for agents that need context and tools."
            body="Run a simple assistant, a specialist code agent, or a team of agents that coordinate across Matrix rooms and bridged networks."
          />
          <div className="mt-10">
            <IconGrid items={platform} />
          </div>
        </div>
      </section>

      <section id="security" className="border-b border-gray-200 py-20 dark:border-gray-800">
        <div className="mx-auto grid max-w-7xl gap-10 px-4 sm:px-6 lg:grid-cols-[0.9fr_1.1fr] lg:px-8">
          <SectionHeading
            eyebrow="Security"
            title="Less magic, more operational control."
            body="Hosted MindRoom instances are designed around explicit room access, visible tool traces, isolated credentials, and deployable infrastructure you can inspect."
          />
          <div className="grid gap-4">
            {security.map((item) => {
              const Icon = item.icon
              return (
                <article key={item.title} className="rounded-lg border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-gray-950">
                  <div className="flex gap-4">
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300">
                      <Icon className="h-5 w-5" />
                    </div>
                    <div>
                      <h3 className="font-semibold text-gray-950 dark:text-white">{item.title}</h3>
                      <p className="mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">{item.body}</p>
                    </div>
                  </div>
                </article>
              )
            })}
          </div>
        </div>
      </section>

      <section className="border-b border-gray-200 bg-gray-950 py-20 text-white dark:border-gray-800">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="grid gap-8 md:grid-cols-3">
            {[
              ['Open source core', 'Run it locally, inspect it, and adapt it to your stack.'],
              ['Hosted control plane', 'Create and manage SaaS instances without hand-editing Helm values.'],
              ['Matrix federation', 'Use protocol-level rooms and bridges instead of trapping work in one app.'],
            ].map(([title, body]) => (
              <div key={title}>
                <h3 className="text-xl font-semibold">{title}</h3>
                <p className="mt-3 text-sm leading-6 text-gray-300">{body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="pricing" className="py-20">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="flex flex-col justify-between gap-8 lg:flex-row lg:items-end">
            <SectionHeading
              eyebrow="Pricing"
              title="Start small, then add rooms and agents."
              body="Use the hosted platform for quick setup or run the open-source stack yourself when you need full control."
            />
            <Link href="/dashboard" className="inline-flex items-center gap-2 self-start rounded-md border border-gray-300 px-4 py-2 text-sm font-semibold text-gray-800 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-100 dark:hover:bg-gray-900 lg:self-end">
              Open dashboard
              <ArrowRight className="h-4 w-4" />
            </Link>
          </div>
          <div className="mt-10 grid gap-4 lg:grid-cols-3">
            {plans.map((plan) => (
              <article
                key={plan.name}
                className={`rounded-lg border p-6 ${
                  plan.highlighted
                    ? 'border-orange-300 bg-orange-50 dark:border-orange-500/40 dark:bg-orange-500/10'
                    : 'border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950'
                }`}
              >
                <h3 className="text-lg font-semibold text-gray-950 dark:text-white">{plan.name}</h3>
                <div className="mt-4 flex items-baseline gap-2">
                  <span className="text-4xl font-semibold text-gray-950 dark:text-white">{plan.price}</span>
                  {plan.price !== '$0' && <span className="text-sm text-gray-500 dark:text-gray-400">monthly</span>}
                </div>
                <p className="mt-3 text-sm leading-6 text-gray-600 dark:text-gray-300">{plan.description}</p>
                <ul className="mt-6 space-y-3">
                  {plan.features.map((feature) => (
                    <li key={feature} className="flex gap-2 text-sm text-gray-700 dark:text-gray-300">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
                      {feature}
                    </li>
                  ))}
                </ul>
                <Link
                  href={plan.href}
                  className={`mt-7 inline-flex w-full items-center justify-center rounded-md px-4 py-2.5 text-sm font-semibold ${
                    plan.highlighted
                      ? 'bg-gray-950 text-white hover:bg-gray-800 dark:bg-white dark:text-gray-950 dark:hover:bg-gray-200'
                      : 'border border-gray-300 text-gray-800 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-100 dark:hover:bg-gray-900'
                  }`}
                >
                  {plan.cta}
                </Link>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="border-y border-gray-200 bg-gray-50 py-16 dark:border-gray-800 dark:bg-gray-900/30">
        <div className="mx-auto flex max-w-7xl flex-col gap-6 px-4 sm:px-6 lg:flex-row lg:items-center lg:justify-between lg:px-8">
          <div>
            <h2 className="text-3xl font-semibold text-gray-950 dark:text-white">Bring agents into the room.</h2>
            <p className="mt-3 max-w-2xl text-base leading-7 text-gray-600 dark:text-gray-300">
              Create a hosted workspace, or inspect the repo and run MindRoom on your own infrastructure.
            </p>
          </div>
          <div className="flex flex-col gap-3 sm:flex-row">
            <Link href="/auth/signup" className="inline-flex items-center justify-center gap-2 rounded-md bg-gray-950 px-5 py-3 text-sm font-semibold text-white hover:bg-gray-800 dark:bg-white dark:text-gray-950 dark:hover:bg-gray-200">
              Start free
              <ArrowRight className="h-4 w-4" />
            </Link>
            <a href="https://github.com/mindroom-ai/mindroom" target="_blank" rel="noopener noreferrer" className="inline-flex items-center justify-center gap-2 rounded-md border border-gray-300 bg-white px-5 py-3 text-sm font-semibold text-gray-800 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 dark:hover:bg-gray-900">
              <Code2 className="h-4 w-4" />
              Read the code
            </a>
          </div>
        </div>
      </section>

      <footer className="py-10">
        <div className="mx-auto flex max-w-7xl flex-col gap-6 px-4 text-sm text-gray-500 dark:text-gray-400 sm:px-6 md:flex-row md:items-center md:justify-between lg:px-8">
          <div className="flex items-center gap-3">
            <MindRoomLogo className="text-gray-500 dark:text-gray-400" size={24} />
            <span>MindRoom</span>
          </div>
          <div className="flex flex-wrap gap-4">
            <Link href="/privacy" className="hover:text-gray-950 dark:hover:text-white">Privacy</Link>
            <Link href="/terms" className="hover:text-gray-950 dark:hover:text-white">Terms</Link>
            <a href="https://github.com/mindroom-ai/mindroom" target="_blank" rel="noopener noreferrer" className="hover:text-gray-950 dark:hover:text-white">GitHub</a>
            <Link href="/auth/login" className="hover:text-gray-950 dark:hover:text-white">Sign in</Link>
          </div>
        </div>
      </footer>
    </main>
  )
}
