'use client'

import Link from 'next/link'
import { Fragment, type ReactNode, useEffect, useRef, useState } from 'react'
import { DarkModeToggle } from '@/components/DarkModeToggle'
import { HeroParticleBackground } from '@/components/landing/HeroParticleBackground'
import { ProductFilm } from '@/components/landing/ProductFilm'
import { MindRoomLogo } from '@/components/MindRoomLogo'
import { useLiquidGlass } from '@/components/glass/useLiquidGlass'
import {
  ArrowRight,
  BookOpen,
  Bot,
  Brain,
  Check,
  Cloud,
  Copy,
  GitBranch,
  Globe,
  Laptop,
  Lock,
  Menu,
  MessageSquare,
  Network,
  Server,
  Shield,
  Smartphone,
  SquareTerminal,
  TabletSmartphone,
  X,
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
const showcaseUrl = `${docsUrl}showcase/`
const chatUrl = 'https://chat.mindroom.chat'
const macAppUrl = `${docsUrl}installation/macos-app/`

type InstallOption = {
  label: string
  icon: LucideIcon
  command: string
  note: ReactNode
  steps: [title: string, body: string][]
  guide: { href: string; label: string }
}

const installOptions: InstallOption[] = [
  {
    label: 'Terminal',
    icon: SquareTerminal,
    command: 'uvx mindroom run',
    note: (
      <>
        Needs{' '}
        <a href="https://docs.astral.sh/uv/getting-started/installation/" className="underline decoration-gray-400 underline-offset-2 hover:text-gray-950 dark:hover:text-white">
          uv
        </a>{' '}
        and a model: an API key, a subscription login, or a local model.
      </>
    ),
    steps: [
      ['Run MindRoom on your computer', 'One command installs it with a starter agent.'],
      ['Approve the pairing link', 'Sign in to MindRoom Chat in your browser and approve it, and MindRoom starts your agents.'],
    ],
    guide: { href: installGuideUrl, label: 'Read the install guide' },
  },
  {
    label: 'macOS app',
    icon: Laptop,
    command: 'brew install --cask mindroom-ai/tap/mindroom',
    note: 'Needs an Apple silicon Mac with macOS 14 or later.',
    steps: [
      ['Open the MindRoom app', 'It installs MindRoom and runs your agents on your Mac in the background, with one-click local models.'],
      ['Connect your chat account', 'Approve the pairing link without leaving the app.'],
    ],
    guide: { href: macAppUrl, label: 'Read the macOS app guide' },
  },
]

const chatApps: { label: string; href?: string; icon: LucideIcon; beta?: boolean }[] = [
  { label: 'Web', href: chatUrl, icon: Globe },
  { label: 'Mac', href: macAppUrl, icon: Laptop },
  { label: 'iPhone & iPad', href: 'https://apps.apple.com/us/app/mindroom-ai/id6760272172', icon: TabletSmartphone },
  { label: 'Android', icon: Smartphone, beta: true },
]

const navLinks = [
  { href: '#why', label: 'Why MindRoom' },
  { href: '#self-host', label: 'Self-host' },
  { href: '#hosted', label: 'Hosted' },
]

const menuLinks: { href: string; label: string; external?: boolean; newTab?: boolean }[] = [
  { href: chatUrl, label: 'Open MindRoom Chat', external: true },
  ...navLinks,
  { href: showcaseUrl, label: 'Showcase', external: true },
  { href: docsUrl, label: 'Docs', external: true },
  { href: githubUrl, label: 'GitHub', external: true, newTab: true },
  { href: '/auth/login', label: 'Sign in' },
]

const heroFacts = ['Open source, Apache 2.0', 'Any model, local or cloud', 'Self-host the whole stack']

const personalUses = [
  'Plan a family trip, from flights to a packing list.',
  'Keep your calendar, reminders, and to-do lists in order, by voice.',
  'Keep notes, a journal, and memories you can find months later.',
  'Follow the topics you care about, with a digest only when something is new.',
  'Look after your homelab and smart home, with approval for the actions you choose.',
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
    body: 'Personal agents and shared team agents connect to 100+ tools, including email, calendar, Slack, Jira, GitHub, and any MCP server, and search your own documents. Even from a server across the world, they can use a computer you pair.',
    icon: Bot,
    href: `${docsUrl}#agents-that-know-you-and-your-work`,
  },
  {
    title: 'Private where it matters',
    body: 'Pick a model per agent: a local one for your most personal data, a frontier one for coding. With local memory and your own server, nothing that agent sees leaves your home, and what you tell a private agent stays out of shared ones.',
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
    body: 'One-tap approval for the actions you choose, sandboxed code execution (on by default when hosted), and opt-in end-to-end encryption on Matrix, the open standard governments use for secure messaging.',
    icon: Shield,
    href: `${docsUrl}#safe-to-give-real-access`,
  },
  {
    title: 'A chat app built for agents',
    body: 'MindRoom builds its own client for the web, Mac, iPhone, iPad, and Android (in beta), so agents can show live tool traces, ask for approval, open interactive canvases, join voice calls, and work in a real browser you can take over.',
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
    title: 'Your computer + MindRoom Chat',
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
    body: 'Let us run it for you, from your own keys to a larger workspace.',
    icon: Cloud,
    href: '#hosted',
    cta: 'See hosted plans',
  },
]

const plans: PricePlan[] = [
  {
    name: 'Your own keys',
    price: '$10',
    description: 'Hosted MindRoom that uses your model API keys, so AI usage is billed by your provider.',
    features: ['Hosted instance', 'Your own model API keys', '10 GB storage'],
    cta: 'Create account',
    href: '/auth/signup?plan=byok',
  },
  {
    name: 'Hobby',
    price: '$20',
    description: 'Hosted MindRoom with AI usage included, so no API keys needed.',
    features: ['Hosted instance', '$15 of AI credit every month', '10 GB storage'],
    cta: 'Create account',
    href: '/auth/signup?plan=hobby',
  },
  {
    name: 'Pro',
    price: '$200',
    description: 'Larger hosted workspace with ten times the included AI usage.',
    features: ['Larger instance with 25 GB storage', '$150 of AI credit every month', 'Priority support'],
    cta: 'Create account',
    href: '/auth/signup?plan=pro',
  },
]

const footerLinks = [
  { href: docsUrl, label: 'Docs' },
  { href: installGuideUrl, label: 'Install guide' },
  { href: showcaseUrl, label: 'Showcase' },
  { href: `${docsUrl}architecture/security-posture/`, label: 'Security' },
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

function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false)
  const commandRef = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 2000)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command)
      setCopied(true)
    } catch {
      // Browsers can refuse clipboard access, so select the command for a manual copy instead.
      if (commandRef.current) window.getSelection()?.selectAllChildren(commandRef.current)
    }
  }

  return (
    <div className="flex items-center justify-between gap-3 rounded-md bg-gray-950 py-2 pl-4 pr-2 dark:bg-black">
      <pre className="min-w-0 overflow-x-auto whitespace-pre-wrap font-mono text-sm text-gray-100">
        <code>
          <span className="select-none text-emerald-400">$ </span>
          <span ref={commandRef}>
            {/* Wrap long commands only between words, never at a hyphen. */}
            {command.split(' ').map((word, index) => (
              <Fragment key={index}>
                {index > 0 && ' '}
                <span className="whitespace-nowrap">{word}</span>
              </Fragment>
            ))}
          </span>
        </code>
      </pre>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? 'Copied' : 'Copy command'}
        className={`inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-xs font-semibold transition active:scale-95 ${
          copied ? 'bg-emerald-400/15 text-emerald-300' : 'text-gray-300 hover:bg-white/10 hover:text-white'
        }`}
      >
        {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
        <span aria-live="polite">{copied ? 'Copied' : 'Copy'}</span>
      </button>
    </div>
  )
}

function ChatAppLinks() {
  const chipClass = 'inline-flex items-center gap-1.5 rounded-md border border-gray-950/10 bg-white/50 px-2.5 py-1 text-xs font-medium text-gray-700 dark:border-white/12 dark:bg-white/5 dark:text-gray-200'

  return (
    <ul className="mt-3 flex flex-wrap gap-2" aria-label="MindRoom Chat apps">
      {chatApps.map(({ label, href, icon: Icon, beta }) => {
        const content = (
          <>
            <Icon className="h-3.5 w-3.5" />
            {label}
            {beta && (
              <span className="rounded bg-orange-100 px-1 text-[10px] font-semibold uppercase text-orange-700 dark:bg-orange-500/15 dark:text-orange-300">
                Beta
              </span>
            )}
          </>
        )
        return (
          <li key={label}>
            {href ? (
              <a href={href} className={`${chipClass} transition-colors hover:border-gray-950/20 hover:text-gray-950 dark:hover:border-white/24 dark:hover:text-white`}>
                {content}
              </a>
            ) : (
              <span className={chipClass}>{content}</span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

function RunItYourself() {
  const [option, setOption] = useState(installOptions[0])
  const steps = [...option.steps, ['Talk to your agents', 'In MindRoom Chat on the web or in the native apps.']]
  const glass = useLiquidGlass<HTMLDivElement>()

  return (
    <div ref={glass} className="liquid-glass relative overflow-hidden rounded-2xl">
      <div className="flex items-center justify-between border-b border-gray-950/8 px-4 py-3 dark:border-white/10">
        <div className="flex items-center gap-2">
          <span className="h-3 w-3 rounded-full bg-red-400" />
          <span className="h-3 w-3 rounded-full bg-yellow-400" />
          <span className="h-3 w-3 rounded-full bg-green-400" />
        </div>
        <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Run it yourself</span>
      </div>
      <div className="p-5 sm:p-6">
        <div role="group" aria-label="Install with" className="mb-4 inline-flex rounded-lg bg-gray-950/5 p-1 dark:bg-white/8">
          {installOptions.map((item) => {
            const Icon = item.icon
            const selected = item === option
            return (
              <button
                key={item.label}
                type="button"
                aria-pressed={selected}
                onClick={() => setOption(item)}
                className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold transition-colors ${
                  selected ? 'bg-white text-gray-950 shadow-sm dark:bg-white/14 dark:text-white' : 'text-gray-600 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white'
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
                {item.label}
              </button>
            )
          })}
        </div>
        <CopyCommand key={option.label} command={option.command} />
        <p className="mt-2 text-xs leading-5 text-gray-500 dark:text-gray-400">{option.note}</p>
        <ol className="mt-6 space-y-5">
          {steps.map(([title, body], index) => (
            <li key={title} className="flex gap-4">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-orange-100 text-sm font-semibold text-orange-700 dark:bg-orange-500/15 dark:text-orange-300">
                {index + 1}
              </span>
              <div>
                <div className="text-sm font-semibold text-gray-950 dark:text-white">{title}</div>
                <p className="mt-1 text-sm leading-6 text-gray-600 dark:text-gray-300">{body}</p>
                {index === steps.length - 1 && <ChatAppLinks />}
              </div>
            </li>
          ))}
        </ol>
        <a href={option.guide.href} className={`mt-6 ${textLinkClass}`}>
          {option.guide.label}
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

function MobileMenu() {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      buttonRef.current?.focus()
    }
    // A tap outside, or keyboard focus moving to a control the open menu would hide, closes it.
    const closeOutside = (event: Event) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', closeOnEscape)
    document.addEventListener('pointerdown', closeOutside)
    document.addEventListener('focusin', closeOutside)
    return () => {
      document.removeEventListener('keydown', closeOnEscape)
      document.removeEventListener('pointerdown', closeOutside)
      document.removeEventListener('focusin', closeOutside)
    }
  }, [open])

  const linkClass = 'block rounded-md px-3 py-2.5 text-base font-medium text-gray-700 hover:bg-gray-100 hover:text-gray-950 dark:text-gray-200 dark:hover:bg-white/8 dark:hover:text-white'

  return (
    <div ref={containerRef} className="xl:hidden">
      <button
        ref={buttonRef}
        type="button"
        aria-label="Menu"
        aria-expanded={open}
        aria-controls={open ? 'mobile-menu' : undefined}
        onClick={() => setOpen(!open)}
        className="flex h-9 w-9 items-center justify-center rounded-md text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-950 dark:text-gray-300 dark:hover:bg-white/8 dark:hover:text-white"
      >
        {open ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
      </button>
      {open && (
        <div id="mobile-menu" className="absolute inset-x-0 top-full max-h-[calc(100dvh-4rem)] overflow-y-auto overscroll-contain border-b border-gray-200 bg-white px-4 py-3 shadow-lg dark:border-gray-800 dark:bg-gray-950">
          {menuLinks.map((link) =>
            link.external ? (
              <a
                key={link.label}
                href={link.href}
                onClick={() => setOpen(false)}
                className={linkClass}
                {...(link.newTab ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
              >
                {link.label}
              </a>
            ) : (
              <Link key={link.label} href={link.href} onClick={() => setOpen(false)} className={linkClass}>
                {link.label}
              </Link>
            ),
          )}
        </div>
      )}
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
            <span className="text-lg font-semibold max-[359px]:sr-only">MindRoom</span>
          </Link>
          <div className="hidden items-center gap-7 xl:flex">
            {navLinks.map((link) => (
              <Link key={link.href} href={link.href} className="text-sm font-medium text-gray-600 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white">
                {link.label}
              </Link>
            ))}
          </div>
          <div className="flex items-center gap-1 sm:gap-3">
            <a href={showcaseUrl} className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white xl:inline-flex">
              Showcase
            </a>
            <a href={docsUrl} className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white sm:inline-flex">
              Docs
            </a>
            <a href={githubUrl} target="_blank" rel="noopener noreferrer" className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white lg:inline-flex">
              GitHub
            </a>
            <DarkModeToggle />
            <Link href="/auth/login" className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-950 dark:text-gray-300 dark:hover:bg-white/8 dark:hover:text-white md:inline-flex">
              Sign in
            </Link>
            <a href={chatUrl} className="hidden items-center gap-2 whitespace-nowrap rounded-md border border-gray-300 bg-white/70 px-3 py-2 text-sm font-semibold text-gray-800 transition-colors hover:bg-gray-50 dark:border-white/14 dark:bg-white/5 dark:text-white/88 dark:hover:bg-white/10 sm:inline-flex">
              <MessageSquare className="h-4 w-4" />
              Open Chat
            </a>
            <a href={installGuideUrl} className="inline-flex items-center gap-2 whitespace-nowrap rounded-md border border-gray-950/10 bg-gray-950 px-3 py-2 text-sm font-semibold text-white transition-colors hover:bg-gray-800 dark:border-white dark:bg-white dark:text-gray-950 dark:hover:bg-gray-200 sm:px-4">
              Get started
              <ArrowRight className="hidden h-4 w-4 sm:block" />
            </a>
            <MobileMenu />
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
              Already have agents?{' '}
              <a href={chatUrl} className="font-semibold text-orange-700 hover:text-orange-800 dark:text-orange-300 dark:hover:text-orange-200">
                Open MindRoom Chat
              </a>
            </p>
            <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
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
            body="Nothing to install: bring your own model keys or include AI usage in your plan, and try MindRoom free for 3 days."
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
                  <span className="text-sm text-gray-500 dark:text-gray-400">monthly</span>
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
          <p className="mt-5 text-sm text-gray-600 dark:text-gray-300">
            Need custom limits, dedicated infrastructure, or an on-premises deployment?{' '}
            <a href="mailto:sales@mindroom.chat" className="font-semibold text-gray-950 underline decoration-gray-400 underline-offset-2 hover:decoration-gray-950 dark:text-white dark:hover:decoration-white">
              Email sales@mindroom.chat
            </a>
          </p>
        </div>
      </section>

      <section className="border-y border-gray-200 bg-gray-950 py-16 text-white dark:border-gray-800 dark:bg-gray-900">
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
