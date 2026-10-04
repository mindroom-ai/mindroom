'use client'

import Link from 'next/link'
import { useState } from 'react'
import { DarkModeToggle } from '@/components/DarkModeToggle'
import { HeroParticleBackground } from '@/components/landing/HeroParticleBackground'
import { ProductFilm } from '@/components/landing/ProductFilm'
import { MindRoomLogo } from '@/components/MindRoomLogo'
import {
  ArrowRight,
  BookOpen,
  Bot,
  Brain,
  Check,
  Cloud,
  Copy,
  GitBranch,
  Laptop,
  Lock,
  MessageSquare,
  Network,
  Server,
  Shield,
  type LucideIcon,
} from 'lucide-react'

type IconItem = {
  title: string
  body: string
  icon: LucideIcon
  href: string
}

type PricePlan = {
  name: string
  price: string
  description: string
  features: string[]
  cta: string
  href: string
}

const docsUrl = 'https://docs.mindroom.chat/'
const githubUrl = 'https://github.com/mindroom-ai/mindroom'
const installGuideUrl = `${docsUrl}getting-started/`

const navLinks = [
  { href: '#why', label: 'Why MindRoom' },
  { href: '#self-host', label: 'Self-host' },
  { href: '#hosted', label: 'Hosted' },
]

const heroFacts = ['Open source, Apache 2.0', 'Any model, local or cloud', 'Self-host the whole stack']

const personalUses = [
  'Plan a family trip, from flights to a packing list.',
  'Keep your calendar, reminders, and to-do lists in order, by voice.',
  'Keep notes, a journal, and memories you can find months later.',
  'Follow the topics you care about, with a digest only when something is new.',
  'Look after your homelab and smart home, with approval for anything risky.',
  'Build quick tools and scripts in the agent’s own workspace.',
]

const workUses = [
  'Find anything across email, chat, documents, tickets, and code, with sources.',
  'Get a morning briefing, or a summary of your week before a one-on-one.',
  'Write status updates from what actually happened in chat and the tracker.',
  'Turn a question into a report or a presentation built from your own documents.',
  'Triage the inbox and draft emails, approving each one before it is sent.',
  'Join a new team and ask its agent how the work fits together.',
]

const reasons: IconItem[] = [
  {
    title: 'Connected to your tools and documents',
    body: 'Personal agents and shared team agents connect to 100+ tools, including email, calendar, Slack, Jira, GitHub, and any MCP server, and search your own documents.',
    icon: Bot,
    href: `${docsUrl}#agents-that-know-you-and-your-work`,
  },
  {
    title: 'Private where it matters',
    body: 'Pick a model per agent: a local one for your most personal data, a frontier one for coding. With local memory and your own server, nothing that agent sees leaves your home.',
    icon: Lock,
    href: `${docsUrl}#private-where-it-matters`,
  },
  {
    title: 'Memory that keeps improving',
    body: 'Agents keep what matters from every conversation, get better as more people use them, and can turn work they repeat into reusable skills.',
    icon: Brain,
    href: `${docsUrl}#they-remember-and-keep-improving`,
  },
  {
    title: 'Safe to give real access',
    body: 'One-tap approval for anything risky, sandboxed code execution, and end-to-end encryption on Matrix, the open standard governments use for secure messaging.',
    icon: Shield,
    href: `${docsUrl}#safe-to-give-real-access`,
  },
  {
    title: 'A chat app built for agents',
    body: 'MindRoom builds its own client for web, iPhone, iPad, and Mac, so agents can show live tool traces, ask for approval, join voice calls, and work in a browser you can take over.',
    icon: MessageSquare,
    href: `${docsUrl}#a-chat-app-built-for-agents`,
  },
  {
    title: 'Works where you already are',
    body: 'Bridges bring the same agents to Slack, Telegram, WhatsApp, and Discord, and an MCP gateway brings them to Claude Code and Codex.',
    icon: Network,
    href: `${docsUrl}#works-where-you-already-are`,
  },
]

const setups: (IconItem & { cta: string })[] = [
  {
    title: 'Your computer, hosted chat',
    body: 'Run the backend on your machine with one command and pair it with MindRoom Chat. Your agents, memory, and keys live on your machine, and there is no server to run.',
    icon: Laptop,
    href: installGuideUrl,
    cta: 'Install guide',
  },
  {
    title: 'Everything on your servers',
    body: 'Run the Matrix server, the chat app, and MindRoom yourself with Docker Compose, or on Kubernetes with the Helm charts.',
    icon: Server,
    href: `${docsUrl}deployment/`,
    cta: 'Deployment guide',
  },
  {
    title: 'Hosted MindRoom',
    body: 'Let us run it for you, from a free plan to a larger workspace.',
    icon: Cloud,
    href: '#hosted',
    cta: 'See hosted plans',
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
    name: 'Your own keys',
    price: '$10',
    description: 'Hosted MindRoom that uses your model API keys, so AI usage is billed by your provider.',
    features: ['Hosted instance', 'Your own model API keys', 'All integrations'],
    cta: 'Create account',
    href: '/auth/signup?plan=byok',
  },
  {
    name: 'Hobby',
    price: '$20',
    description: 'Hosted MindRoom with included monthly AI usage.',
    features: ['Hosted instance', '$15 included monthly AI usage', 'All integrations'],
    cta: 'Create account',
    href: '/auth/signup?plan=hobby',
  },
  {
    name: 'Pro',
    price: '$200',
    description: 'Larger hosted workspace with a higher included AI budget.',
    features: ['Larger instance', '$150 included monthly AI usage', 'Priority support'],
    cta: 'Create account',
    href: '/auth/signup?plan=pro',
  },
]

const footerLinks = [
  { href: docsUrl, label: 'Docs' },
  { href: installGuideUrl, label: 'Install guide' },
  { href: `${docsUrl}showcase/`, label: 'Showcase' },
  { href: githubUrl, label: 'GitHub' },
  { href: 'https://pypi.org/project/mindroom/', label: 'PyPI' },
]

const primaryCtaClass = 'inline-flex items-center justify-center gap-2 rounded-md border border-gray-950/10 bg-gray-950 px-5 py-3 text-sm font-semibold text-white transition-colors hover:bg-gray-800 dark:border-white dark:bg-white dark:text-gray-950 dark:hover:bg-gray-200'
const secondaryCtaClass = 'inline-flex items-center justify-center gap-2 rounded-md border border-gray-300 bg-white/70 px-5 py-3 text-sm font-semibold text-gray-800 transition-colors hover:bg-gray-50 dark:border-white/14 dark:bg-white/5 dark:text-white/88 dark:hover:bg-white/10'
const darkPrimaryCtaClass = 'inline-flex items-center justify-center gap-2 rounded-md border border-white bg-white px-5 py-3 text-sm font-semibold text-gray-950 transition-colors hover:bg-gray-200'
const darkSecondaryCtaClass = 'inline-flex items-center justify-center gap-2 rounded-md border border-white/14 bg-transparent px-5 py-3 text-sm font-semibold text-white/82 transition-colors hover:bg-white/8 hover:text-white'
const textLinkClass = 'inline-flex items-center gap-1 text-sm font-semibold text-orange-700 hover:text-orange-800 dark:text-orange-300 dark:hover:text-orange-200'

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
      <p className="text-sm font-semibold uppercase tracking-normal text-orange-700 dark:text-orange-400">
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

const installCommand = 'uvx mindroom run'

function CopyCommand() {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    await navigator.clipboard.writeText(installCommand)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="flex items-center justify-between gap-3 rounded-md bg-gray-950 py-2 pl-4 pr-2 dark:bg-black">
      <pre className="overflow-x-auto font-mono text-sm text-gray-100">
        <code>
          <span className="select-none text-emerald-400">$ </span>
          {installCommand}
        </code>
      </pre>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? 'Copied' : 'Copy command'}
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-white/10 hover:text-white"
      >
        {copied ? <Check className="h-4 w-4 text-emerald-400" /> : <Copy className="h-4 w-4" />}
      </button>
    </div>
  )
}

function RunItYourself() {
  const steps = [
    ['Run MindRoom on your computer', 'One command installs and starts it, with a starter agent.'],
    ['Approve the pairing link', 'Connect it to your MindRoom Chat account in the browser.'],
    ['Talk to your agents', 'On the web, on iPhone and iPad, or on the Mac.'],
  ]

  return (
    <div className="relative overflow-hidden rounded-lg border border-gray-200 bg-white shadow-xl shadow-gray-200/70 dark:border-gray-800 dark:bg-gray-950 dark:shadow-black/25">
      <div className="flex items-center justify-between border-b border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-900">
        <div className="flex items-center gap-2">
          <span className="h-3 w-3 rounded-full bg-red-400" />
          <span className="h-3 w-3 rounded-full bg-yellow-400" />
          <span className="h-3 w-3 rounded-full bg-green-400" />
        </div>
        <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Run it yourself</span>
      </div>
      <div className="p-5 sm:p-6">
        <CopyCommand />
        <ol className="mt-6 space-y-5">
          {steps.map(([title, body], index) => (
            <li key={title} className="flex gap-4">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-orange-100 text-sm font-semibold text-orange-700 dark:bg-orange-500/15 dark:text-orange-300">
                {index + 1}
              </span>
              <div>
                <div className="text-sm font-semibold text-gray-950 dark:text-white">{title}</div>
                <p className="mt-1 text-sm leading-6 text-gray-600 dark:text-gray-300">{body}</p>
              </div>
            </li>
          ))}
        </ol>
        <a href={installGuideUrl} className={`mt-6 ${textLinkClass}`}>
          Read the install guide
          <ArrowRight className="h-4 w-4" />
        </a>
      </div>
    </div>
  )
}

function UseCaseList({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-gray-950">
      <h3 className="text-lg font-semibold text-gray-950 dark:text-white">{title}</h3>
      <ul className="mt-4 space-y-3">
        {items.map((item) => (
          <li key={item} className="flex gap-3 text-sm leading-6 text-gray-700 dark:text-gray-300">
            <Check className="mt-1 h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
            {item}
          </li>
        ))}
      </ul>
    </div>
  )
}

function ReasonRows({ items }: { items: IconItem[] }) {
  return (
    <div className="grid border-y border-gray-200 dark:border-gray-800 lg:grid-cols-2">
      {items.map((item) => {
        const Icon = item.icon
        return (
          <article
            key={item.title}
            className="flex gap-4 border-b border-gray-200 py-6 last:border-b-0 dark:border-gray-800 lg:odd:pr-8 lg:even:border-l lg:even:pl-8 lg:[&:nth-last-child(2)]:border-b-0"
          >
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-gray-100 text-gray-900 dark:bg-white/8 dark:text-white">
              <Icon className="h-5 w-5" />
            </div>
            <div>
              <h3 className="text-base font-semibold text-gray-950 dark:text-white">
                <a href={item.href} className="group inline-flex items-center gap-1.5 hover:text-orange-700 dark:hover:text-orange-300">
                  {item.title}
                  <ArrowRight className="h-4 w-4 text-gray-400 transition-transform group-hover:translate-x-0.5 group-hover:text-current" />
                </a>
              </h3>
              <p className="mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">{item.body}</p>
            </div>
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
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6 lg:px-8">
          <Link href="/" className="group flex items-center gap-3" aria-label="MindRoom home">
            <MindRoomLogo className="transition-transform duration-200 group-hover:scale-105" size={32} />
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
            <a href={docsUrl} className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white sm:inline-flex">
              Docs
            </a>
            <a href={githubUrl} target="_blank" rel="noopener noreferrer" className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white sm:inline-flex">
              GitHub
            </a>
            <DarkModeToggle />
            <Link href="/auth/login" className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-950 dark:text-gray-300 dark:hover:bg-white/8 dark:hover:text-white sm:inline-flex">
              Sign in
            </Link>
            <a href={installGuideUrl} className="inline-flex items-center gap-2 whitespace-nowrap rounded-md border border-gray-950/10 bg-gray-950 px-3 py-2 text-sm font-semibold text-white transition-colors hover:bg-gray-800 dark:border-white dark:bg-white dark:text-gray-950 dark:hover:bg-gray-200 sm:px-4">
              Get started
              <ArrowRight className="h-4 w-4" />
            </a>
          </div>
        </div>
      </nav>

      <section className="relative overflow-hidden border-b border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-950">
        <HeroParticleBackground />
        <div className="relative z-10 mx-auto max-w-7xl px-4 py-10 sm:px-6 sm:py-12 lg:px-8 lg:py-16">
          <h1 className="text-4xl font-semibold tracking-tight text-gray-950 dark:text-white lg:text-5xl">
            <span className="block text-balance">AI agents that know you and your work,</span>{' '}
            <span className="block text-balance text-gray-500 dark:text-gray-400">in a chat app anyone can use.</span>
          </h1>
          <div className="mt-8 grid gap-10 lg:grid-cols-[1fr_0.9fr] lg:items-start">
          <div>
            <p className="max-w-xl text-pretty text-base leading-7 text-gray-600 dark:text-gray-300 sm:text-lg sm:leading-8">
              A personal agent for your calendar, notes, trips, and homelab, and shared agents for your team&apos;s email, documents, and code.
              Pick a local model for your private life or a frontier model for hard problems.
            </p>
            <div className="mt-7 flex flex-col gap-3 sm:flex-row">
              <a href={installGuideUrl} className={primaryCtaClass}>
                Get started
                <ArrowRight className="h-4 w-4" />
              </a>
              <a href={githubUrl} target="_blank" rel="noopener noreferrer" className={secondaryCtaClass}>
                <GitBranch className="h-4 w-4" />
                View on GitHub
              </a>
            </div>
            <p className="mt-4 text-sm text-gray-600 dark:text-gray-400">
              Rather not run it yourself?{' '}
              <Link href="#hosted" className="font-semibold text-orange-700 hover:text-orange-800 dark:text-orange-300 dark:hover:text-orange-200">
                Try hosted MindRoom
              </Link>
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm font-medium text-gray-500 dark:text-gray-400">
              {heroFacts.map((fact, index) => (
                <div key={fact} className="flex items-center gap-2.5 sm:gap-4">
                  <span className={`h-1 w-1 rounded-full bg-gray-300 dark:bg-gray-700 ${index === 0 ? 'sm:hidden' : ''}`} />
                  <span>{fact}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="relative">
            <RunItYourself />
          </div>
          </div>
        </div>
      </section>

      <section id="use-cases" className="border-b border-gray-200 py-16 dark:border-gray-800">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <SectionHeading
            eyebrow="What people use it for"
            title="Your assistant at home, your team's memory at work."
            body="Personal agents for your own life and shared agents for your team, each with its own model, memory, and access."
          />
          <div className="mt-10">
            <ProductFilm />
          </div>
          <div className="mt-10 grid gap-6 lg:grid-cols-2">
            <UseCaseList title="Personal" items={personalUses} />
            <UseCaseList title="Work" items={workUses} />
          </div>
        </div>
      </section>

      <section id="why" className="border-b border-gray-200 bg-gray-50 py-16 dark:border-gray-800 dark:bg-gray-900/30">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <SectionHeading
            eyebrow="Why MindRoom"
            title="Agents that remember and stay yours."
            body="MindRoom builds the whole stack, from the chat app to the server to the AI backend, so it can be private, extensible, and built for agents from end to end."
          />
          <div className="mt-10">
            <ReasonRows items={reasons} />
          </div>
        </div>
      </section>

      <section id="self-host" className="border-b border-gray-200 py-16 dark:border-gray-800">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <SectionHeading
            eyebrow="Run it your way"
            title="Start on your laptop, grow to your own servers."
            body="MindRoom is open source under the Apache 2.0 license, and every option runs the same software."
          />
          <div className="mt-10 grid gap-6 lg:grid-cols-3">
            {setups.map((setup) => {
              const Icon = setup.icon
              return (
                <article key={setup.title} className="flex flex-col rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-gray-950">
                  <div className="flex h-10 w-10 items-center justify-center rounded-md bg-orange-100 text-orange-700 dark:bg-orange-500/15 dark:text-orange-300">
                    <Icon className="h-5 w-5" />
                  </div>
                  <h3 className="mt-4 text-lg font-semibold text-gray-950 dark:text-white">{setup.title}</h3>
                  <p className="mt-2 flex-1 text-sm leading-6 text-gray-600 dark:text-gray-300">{setup.body}</p>
                  <a href={setup.href} className={`mt-5 ${textLinkClass}`}>
                    {setup.cta}
                    <ArrowRight className="h-4 w-4" />
                  </a>
                </article>
              )
            })}
          </div>
        </div>
      </section>

      <section id="hosted" className="bg-gray-50 py-16 dark:bg-gray-900/30">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <SectionHeading
            eyebrow="Hosted"
            title="Prefer us to host it?"
            body="Nothing to install: start free, bring your own model keys, or include AI usage in your plan."
          />
          <div className="mt-10 overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950">
            {plans.map((plan) => (
              <article
                key={plan.name}
                className="grid gap-5 border-b border-gray-200 p-5 last:border-b-0 dark:border-gray-800 lg:grid-cols-[1fr_0.6fr_1.2fr_10rem] lg:items-center"
              >
                <div>
                  <h3 className="text-lg font-semibold text-gray-950 dark:text-white">{plan.name}</h3>
                  <p className="mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">{plan.description}</p>
                </div>
                <div className="flex items-baseline gap-2">
                  <span className="text-3xl font-semibold text-gray-950 dark:text-white">{plan.price}</span>
                  {plan.price !== '$0' && <span className="text-sm text-gray-500 dark:text-gray-400">monthly</span>}
                </div>
                <ul className="grid gap-2 sm:grid-cols-3 lg:grid-cols-1">
                  {plan.features.map((feature) => (
                    <li key={feature} className="flex gap-2 text-sm text-gray-700 dark:text-gray-300">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
                      {feature}
                    </li>
                  ))}
                </ul>
                <Link
                  href={plan.href}
                  className="inline-flex items-center justify-center rounded-md border border-gray-300 px-4 py-2.5 text-sm font-semibold text-gray-800 transition-colors hover:bg-gray-50 dark:border-white/14 dark:text-gray-100 dark:hover:bg-white/8"
                >
                  {plan.cta}
                </Link>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="border-y border-gray-200 bg-gray-950 py-16 text-white dark:border-gray-800">
        <div className="mx-auto flex max-w-7xl flex-col gap-6 px-4 sm:px-6 lg:flex-row lg:items-center lg:justify-between lg:px-8">
          <div>
            <h2 className="text-3xl font-semibold text-white">Run MindRoom today.</h2>
            <p className="mt-3 max-w-2xl text-base leading-7 text-gray-300">
              Install it with one command, read the docs, or let us host it for you.
            </p>
          </div>
          <div className="flex flex-col gap-3 sm:flex-row">
            <a href={installGuideUrl} className={darkPrimaryCtaClass}>
              <BookOpen className="h-4 w-4" />
              Get started
            </a>
            <a href={githubUrl} target="_blank" rel="noopener noreferrer" className={darkSecondaryCtaClass}>
              <GitBranch className="h-4 w-4" />
              View on GitHub
            </a>
            <Link href="/auth/signup" className={darkSecondaryCtaClass}>
              Try hosted
            </Link>
          </div>
        </div>
      </section>

      <footer className="py-10">
        <div className="mx-auto flex max-w-7xl flex-col gap-6 px-4 text-sm text-gray-500 dark:text-gray-400 sm:px-6 md:flex-row md:items-center md:justify-between lg:px-8">
          <div className="flex items-center gap-3">
            <MindRoomLogo size={24} />
            <span>MindRoom is open source under the Apache 2.0 license.</span>
          </div>
          <div className="flex flex-wrap gap-4">
            {footerLinks.map((link) => (
              <a key={link.label} href={link.href} className="hover:text-gray-950 dark:hover:text-white">
                {link.label}
              </a>
            ))}
            <Link href="/privacy" className="hover:text-gray-950 dark:hover:text-white">Privacy</Link>
            <Link href="/terms" className="hover:text-gray-950 dark:hover:text-white">Terms</Link>
            <Link href="/auth/login" className="hover:text-gray-950 dark:hover:text-white">Sign in</Link>
          </div>
        </div>
      </footer>
    </main>
  )
}
