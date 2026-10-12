'use client'

import Link from 'next/link'
import { Fragment, type ReactNode, useEffect, useRef, useState } from 'react'
import { DarkModeToggle } from '@/components/DarkModeToggle'
import { HeroParticleBackground } from '@/components/landing/HeroParticleBackground'
import { ProductFilm } from '@/components/landing/ProductFilm'
import { ProductPreview } from '@/components/landing/ProductPreview'
import { AgentAvatar, type AvatarName } from '@/components/landing/AgentAvatar'
import { MindRoomLogo } from '@/components/MindRoomLogo'
import { useLiquidGlass } from '@/components/glass/useLiquidGlass'
import {
  ArrowRight,
  BookOpen,
  Check,
  Cloud,
  Copy,
  GitBranch,
  Globe,
  Laptop,
  Menu,
  MessageSquare,
  Server,
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

type UseCase = { text: string; avatar: AvatarName }

type Reason = {
  title: string
  body: string
  avatar: AvatarName
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
      ['Open the MindRoom app', 'It installs MindRoom and offers one-click local models.'],
      ['Connect your chat account', 'Approve the pairing link without leaving the app, then click Install and Start Agents to run them in the background.'],
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

const personalUses: UseCase[] = [
  { text: 'Plan a family trip, from flights to a packing list.', avatar: 'helper' },
  { text: 'Keep your calendar, reminders, and to-do lists in order, by voice.', avatar: 'planner' },
  { text: 'Keep notes, a journal, and memories you can find months later.', avatar: 'writer' },
  { text: 'Follow the topics you care about, with a digest only when something is new.', avatar: 'news' },
  { text: 'Look after your homelab and smart home, with approval for the actions you choose.', avatar: 'home' },
  { text: 'Build quick tools and scripts in the agent’s own workspace.', avatar: 'builder' },
]

const workUses: UseCase[] = [
  { text: 'Find anything across email, chat, documents, tickets, and code, with sources.', avatar: 'research' },
  { text: 'Get a morning briefing, or a summary of your week before a one-on-one.', avatar: 'general' },
  { text: 'Write status updates from what actually happened in chat and the tracker.', avatar: 'summary' },
  { text: 'Turn a question into a report or a presentation built from your own documents.', avatar: 'data' },
  { text: 'Triage the inbox and draft emails, approving each one before it is sent.', avatar: 'email' },
  { text: 'Join a new team and ask its agent how the work fits together.', avatar: 'router' },
]

const reasons: Reason[] = [
  {
    title: 'Connected to your tools and documents',
    body: 'Personal agents and shared team agents connect to 100+ tools, including email, calendar, Slack, Jira, GitHub, and any MCP server, and search your own documents. Even from a server across the world, they can use a computer you pair.',
    avatar: 'code',
    href: `${docsUrl}#agents-that-know-you-and-your-work`,
  },
  {
    title: 'Private where it matters',
    body: 'Pick a model per agent: a local one for your most personal data, a frontier one for coding. With local memory and your own server, nothing that agent sees leaves your home, and what you tell a private agent stays out of shared ones.',
    avatar: 'security',
    href: `${docsUrl}#private-where-it-matters`,
  },
  {
    title: 'Memory that keeps improving',
    body: 'Agents keep what matters from every conversation, get better as more people use them, and can turn work they repeat into reusable skills.',
    avatar: 'mind',
    href: `${docsUrl}#they-remember-and-keep-improving`,
  },
  {
    title: 'Safe to give real access',
    body: 'One-tap approval for the actions you choose, sandboxed code execution (on by default in hosted MindRoom), and opt-in end-to-end encryption on Matrix, the open standard governments use for secure messaging.',
    avatar: 'analyst',
    href: `${docsUrl}#safe-to-give-real-access`,
  },
  {
    title: 'A chat app built for agents',
    body: 'MindRoom builds its own client for the web, Mac, iPhone, iPad, and Android (in beta), so agents can show live tool traces, ask for approval, open interactive canvases, join voice calls, and work in a real browser you can take over.',
    avatar: 'computer',
    href: `${docsUrl}#a-chat-app-built-for-agents`,
  },
  {
    title: 'Works where you already are',
    body: 'Bridges bring the same agents to Slack, Telegram, WhatsApp, and Discord, and an MCP gateway brings them to Claude Code and Codex.',
    avatar: 'phone',
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

const [localSetup, ...otherSetups] = setups

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

const primaryCtaClass = 'inline-flex items-center justify-center gap-2 rounded-lg bg-brand-600 px-5 py-3 text-sm font-semibold text-white shadow-sm shadow-brand-900/20 transition-colors hover:bg-brand-700 dark:bg-brand-400 dark:text-brand-950 dark:hover:bg-brand-300'
const secondaryCtaClass = 'inline-flex items-center justify-center gap-2 rounded-lg border border-gray-300 bg-white/70 px-5 py-3 text-sm font-semibold text-gray-800 transition-colors hover:bg-gray-50 dark:border-white/14 dark:bg-white/5 dark:text-white/88 dark:hover:bg-white/10'
const darkPrimaryCtaClass = 'inline-flex items-center justify-center gap-2 rounded-md border border-white bg-white px-5 py-3 text-sm font-semibold text-gray-950 transition-colors hover:bg-gray-200'
const darkSecondaryCtaClass = 'inline-flex items-center justify-center gap-2 rounded-md border border-white/14 bg-transparent px-5 py-3 text-sm font-semibold text-white/82 transition-colors hover:bg-white/8 hover:text-white'
const textLinkClass = 'inline-flex items-center gap-1 text-sm font-semibold text-brand-700 hover:text-brand-800 dark:text-brand-300 dark:hover:text-brand-200'

// The film's typographic accent: a phrase set in italic serif, in MindRoom Chat's violet.
function Em({ children }: { children: ReactNode }) {
  return <em className="font-serif text-[1.08em] font-normal italic tracking-normal text-brand-600 dark:text-brand-300">{children}</em>
}

function SectionHeading({ title, body }: { title: ReactNode; body: string }) {
  return (
    <div className="max-w-2xl">
      <h2 className="text-balance text-3xl font-semibold leading-[1.1] tracking-[-0.03em] text-gray-950 dark:text-white sm:text-[2.75rem]">
        {title}
      </h2>
      <p className="mt-5 text-pretty text-base leading-7 text-gray-600 dark:text-gray-300 sm:text-lg sm:leading-8">
        {body}
      </p>
    </div>
  )
}

function CopyCommand({ command, large = false }: { command: string; large?: boolean }) {
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
    <div
      className={`flex items-center justify-between gap-3 bg-gray-950 pr-2 dark:bg-black ${
        large ? 'rounded-xl py-3 pl-5 shadow-lg shadow-brand-950/20 dark:ring-1 dark:ring-white/12' : 'rounded-md py-2 pl-4'
      }`}
    >
      <pre className={`min-w-0 overflow-x-auto whitespace-pre-wrap font-mono text-gray-100 ${large ? 'text-base' : 'text-sm'}`}>
        <code>
          <span className="select-none text-brand-300">$ </span>
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
              <span className="rounded bg-brand-100 px-1 text-[11px] font-semibold uppercase text-brand-700 dark:bg-brand-400/15 dark:text-brand-300">
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

function RunItYourself({ setup }: { setup: IconItem }) {
  const [option, setOption] = useState(installOptions[0])
  const steps = [...option.steps, ['Talk to your agents', 'In MindRoom Chat on the web or in the native apps.']]
  const glass = useLiquidGlass<HTMLDivElement>()
  const Icon = setup.icon

  return (
    <div ref={glass} className="liquid-glass relative overflow-hidden rounded-2xl">
      <div className="border-b border-gray-950/8 px-5 py-5 dark:border-white/10 sm:px-6">
        <div className="flex items-center gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-100 text-brand-700 dark:bg-brand-400/15 dark:text-brand-300">
            <Icon className="h-5 w-5" />
          </span>
          <h3 className="text-lg font-semibold text-gray-950 dark:text-white">{setup.title}</h3>
        </div>
        <p className="mt-3 text-sm leading-6 text-gray-600 dark:text-gray-300">{setup.body}</p>
      </div>
      <div className="p-5 sm:p-6">
        <div role="group" aria-label="Install with" className="mb-4 inline-flex rounded-lg bg-gray-950/5 p-1 dark:bg-white/8">
          {installOptions.map((item) => {
            const ItemIcon = item.icon
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
                <ItemIcon className="h-3.5 w-3.5" />
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
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-100 font-mono text-xs font-semibold text-brand-700 dark:bg-brand-400/15 dark:text-brand-300">
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

function UseCaseList({ title, items }: { title: string; items: UseCase[] }) {
  return (
    <div>
      <h3 className="text-xl font-semibold tracking-tight text-gray-950 dark:text-white">{title}</h3>
      <ul className="mt-4 divide-y divide-gray-950/8 border-t border-gray-950/8 dark:divide-white/8 dark:border-white/8">
        {items.map((item) => (
          <li key={item.text} className="flex items-center gap-4 py-3.5">
            <AgentAvatar name={item.avatar} size={40} />
            <span className="text-[15px] leading-6 text-gray-700 dark:text-gray-300">{item.text}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function ReasonList({ items }: { items: Reason[] }) {
  return (
    <div className="divide-y divide-gray-950/8 border-y border-gray-950/8 dark:divide-white/8 dark:border-white/8">
      {items.map((item) => (
        <article key={item.title} className="flex gap-5 py-7">
          <AgentAvatar name={item.avatar} size={52} />
          <div className="min-w-0">
            <h3 className="text-lg font-semibold tracking-tight text-gray-950 dark:text-white">
              <a href={item.href} className="group hover:text-brand-700 dark:hover:text-brand-300">
                {item.title}
                <ArrowRight className="ml-1.5 inline h-4 w-4 align-[-2px] text-gray-400 transition-transform group-hover:translate-x-0.5 group-hover:text-current" />
              </a>
            </h3>
            <p className="mt-2 text-[15px] leading-7 text-gray-600 dark:text-gray-300">{item.body}</p>
          </div>
        </article>
      ))}
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
      <nav className="sticky top-0 z-50 border-b border-gray-950/8 bg-white/80 backdrop-blur-xl dark:border-white/8 dark:bg-gray-950/80">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3 sm:px-6 lg:px-8">
          <div className="flex flex-1 items-center">
            <Link href="/" className="group flex items-center gap-3" aria-label="MindRoom home">
              <MindRoomLogo className="transition-transform duration-200 group-hover:scale-105" size={32} />
              <span className="text-lg font-semibold tracking-tight max-[359px]:sr-only">MindRoom</span>
            </Link>
          </div>
          <div className="hidden items-center gap-6 xl:flex">
            {navLinks.map((link) => (
              <Link key={link.href} href={link.href} className="text-sm font-medium text-gray-600 transition-colors hover:text-gray-950 dark:text-gray-300 dark:hover:text-white">
                {link.label}
              </Link>
            ))}
            <a href={showcaseUrl} className="text-sm font-medium text-gray-600 transition-colors hover:text-gray-950 dark:text-gray-300 dark:hover:text-white">
              Showcase
            </a>
            <a href={docsUrl} className="text-sm font-medium text-gray-600 transition-colors hover:text-gray-950 dark:text-gray-300 dark:hover:text-white">
              Docs
            </a>
            <a href={githubUrl} target="_blank" rel="noopener noreferrer" className="text-sm font-medium text-gray-600 transition-colors hover:text-gray-950 dark:text-gray-300 dark:hover:text-white">
              GitHub
            </a>
          </div>
          <div className="flex flex-1 items-center justify-end gap-1 sm:gap-3">
            <a href={docsUrl} className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 transition-colors hover:text-gray-950 dark:text-gray-300 dark:hover:text-white sm:inline-flex xl:hidden">
              Docs
            </a>
            <a href={githubUrl} target="_blank" rel="noopener noreferrer" className="hidden rounded-md px-3 py-2 text-sm font-medium text-gray-600 transition-colors hover:text-gray-950 dark:text-gray-300 dark:hover:text-white lg:inline-flex xl:hidden">
              GitHub
            </a>
            <DarkModeToggle />
            <Link href="/auth/login" className="hidden whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-950 dark:text-gray-300 dark:hover:bg-white/8 dark:hover:text-white md:inline-flex">
              Sign in
            </Link>
            <a href={chatUrl} className="hidden items-center gap-2 whitespace-nowrap rounded-lg border border-gray-300 bg-white/70 px-3 py-2 text-sm font-semibold text-gray-800 transition-colors hover:bg-gray-50 dark:border-white/14 dark:bg-white/5 dark:text-white/88 dark:hover:bg-white/10 sm:inline-flex">
              <MessageSquare className="h-4 w-4" />
              Open Chat
            </a>
            <a href={installGuideUrl} className="inline-flex items-center gap-2 whitespace-nowrap rounded-lg bg-brand-600 px-3 py-2 text-sm font-semibold text-white transition-colors hover:bg-brand-700 dark:bg-brand-400 dark:text-brand-950 dark:hover:bg-brand-300 sm:px-4">
              Get started
              <ArrowRight className="hidden h-4 w-4 sm:block" />
            </a>
            <MobileMenu />
          </div>
        </div>
      </nav>

      <section className="relative overflow-hidden border-b border-gray-950/8 bg-paper dark:border-white/8 dark:bg-gray-950">
        <HeroParticleBackground />
        <div className="relative z-10 mx-auto max-w-7xl px-4 pt-14 sm:px-6 sm:pt-20 lg:px-8 lg:pt-24">
          <div className="mx-auto max-w-6xl text-center">
            <h1 className="text-balance text-[2.6rem] font-semibold leading-[1.05] tracking-[-0.03em] text-gray-950 dark:text-white sm:text-6xl lg:text-[4.1rem]">
              <span className="lg:block">AI agents that know you and your work,</span>{' '}
              <span className="lg:block">in a <Em>chat app anyone can use.</Em></span>
            </h1>
            <p className="mx-auto mt-6 max-w-2xl text-pretty text-base leading-7 text-gray-600 dark:text-gray-300 sm:text-lg sm:leading-8">
              A personal agent for your calendar, notes, trips, and homelab, and shared agents for your team&apos;s email, documents, and code.
              Pick a local model for your private life or a frontier model for hard problems.
            </p>
            <div className="mx-auto mt-9 max-w-md text-left">
              <CopyCommand command={installOptions[0].command} large />
              <p className="mt-2.5 text-center text-xs leading-5 text-gray-500 dark:text-gray-400">{installOptions[0].note}</p>
            </div>
            <div className="mt-7 flex flex-col justify-center gap-3 sm:flex-row">
              <a href={installGuideUrl} className={primaryCtaClass}>
                Get started
                <ArrowRight className="h-4 w-4" />
              </a>
              <a href={githubUrl} target="_blank" rel="noopener noreferrer" className={secondaryCtaClass}>
                <GitBranch className="h-4 w-4" />
                View on GitHub
              </a>
            </div>
            <div className="mt-6 flex flex-col items-center gap-1 text-sm text-gray-600 dark:text-gray-400 sm:flex-row sm:justify-center sm:gap-5">
              <p>
                Already have agents?{' '}
                <a href={chatUrl} className="font-semibold text-brand-700 hover:text-brand-800 dark:text-brand-300 dark:hover:text-brand-200">
                  Open MindRoom Chat
                </a>
              </p>
              <span className="hidden h-1 w-1 rounded-full bg-gray-300 dark:bg-gray-700 sm:block" />
              <p>
                Rather not run it yourself?{' '}
                <Link href="#hosted" className="font-semibold text-brand-700 hover:text-brand-800 dark:text-brand-300 dark:hover:text-brand-200">
                  Try hosted MindRoom
                </Link>
              </p>
            </div>
          </div>
          <div className="relative mx-auto mt-14 max-w-6xl sm:mt-16">
            <ProductPreview />
          </div>
          <ul className="flex flex-wrap justify-center gap-x-7 gap-y-2 py-10 text-sm font-medium text-gray-600 dark:text-gray-400">
            {heroFacts.map((fact) => (
              <li key={fact} className="flex items-center gap-2">
                <Check className="h-3.5 w-3.5 text-brand-600 dark:text-brand-300" />
                {fact}
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section id="use-cases" className="border-b border-gray-950/8 py-20 dark:border-white/8 sm:py-24">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <SectionHeading
            title={<>Your assistant at <Em>home,</Em> your team&apos;s memory at <Em>work.</Em></>}
            body="Personal agents for your own life and shared agents for your team, each with its own model, memory, and access."
          />
          <div className="mt-12">
            <ProductFilm />
          </div>
          <div className="mt-16 grid gap-12 lg:grid-cols-2 lg:gap-16">
            <UseCaseList title="Personal" items={personalUses} />
            <UseCaseList title="Work" items={workUses} />
          </div>
        </div>
      </section>

      <section id="why" className="border-b border-gray-950/8 bg-paper py-20 dark:border-white/8 dark:bg-gray-900/40 sm:py-24">
        <div className="mx-auto grid max-w-7xl gap-12 px-4 sm:px-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:gap-16 lg:px-8">
          <div className="lg:sticky lg:top-28 lg:self-start">
            <SectionHeading
              title={<>Agents that remember and stay <Em>yours.</Em></>}
              body="MindRoom builds the whole stack, from the chat app to the server to the AI backend, so it can be private, extensible, and built for agents from end to end."
            />
          </div>
          <ReasonList items={reasons} />
        </div>
      </section>

      <section id="self-host" className="border-b border-gray-950/8 py-20 dark:border-white/8 sm:py-24">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <SectionHeading
            title={<>Start on your laptop, grow to your <Em>own servers.</Em></>}
            body="MindRoom is open source under the Apache 2.0 license, and every option runs the same software."
          />
          <div className="mt-12 grid gap-10 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] lg:items-start">
            <RunItYourself setup={localSetup} />
            <div className="divide-y divide-gray-950/8 border-y border-gray-950/8 dark:divide-white/8 dark:border-white/8">
              {otherSetups.map((setup) => {
                const Icon = setup.icon
                return (
                  <article key={setup.title} className="py-7">
                    <div className="flex items-center gap-3">
                      <Icon className="h-5 w-5 shrink-0 text-brand-600 dark:text-brand-300" />
                      <h3 className="text-lg font-semibold text-gray-950 dark:text-white">{setup.title}</h3>
                    </div>
                    <p className="mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">{setup.body}</p>
                    <a href={setup.href} className={`mt-4 ${textLinkClass}`}>
                      {setup.cta}
                      <ArrowRight className="h-4 w-4" />
                    </a>
                  </article>
                )
              })}
            </div>
          </div>
        </div>
      </section>

      <section id="hosted" className="bg-paper py-20 dark:bg-gray-900/40 sm:py-24">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <SectionHeading
            title={<>Prefer us to <Em>host it?</Em></>}
            body="Nothing to install: bring your own model keys or include AI usage in your plan, and try MindRoom free for 3 days."
          />
          <div className="mt-12 grid divide-y divide-gray-950/8 overflow-hidden rounded-2xl bg-white shadow-[0_24px_60px_-28px_rgba(47,33,97,0.25)] dark:divide-white/8 dark:bg-gray-950 dark:shadow-none dark:outline dark:outline-1 dark:outline-white/10 lg:grid-cols-3 lg:divide-x lg:divide-y-0">
            {plans.map((plan) => (
              <article key={plan.name} className="flex flex-col p-7 sm:p-8">
                <h3 className="text-lg font-semibold text-gray-950 dark:text-white">{plan.name}</h3>
                <p className="mt-1.5 text-sm leading-6 text-gray-600 dark:text-gray-300 lg:min-h-12">{plan.description}</p>
                <div className="mt-6 flex items-baseline gap-2">
                  <span className="text-5xl font-semibold tracking-[-0.03em] text-gray-950 dark:text-white">{plan.price}</span>
                  <span className="text-sm text-gray-500 dark:text-gray-400">monthly</span>
                </div>
                <ul className="mt-6 flex-1 space-y-2.5">
                  {plan.features.map((feature) => (
                    <li key={feature} className="flex gap-2.5 text-sm text-gray-700 dark:text-gray-300">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-brand-600 dark:text-brand-300" />
                      {feature}
                    </li>
                  ))}
                </ul>
                <Link
                  href={plan.href}
                  className="mt-8 inline-flex items-center justify-center rounded-lg border border-gray-300 px-4 py-2.5 text-sm font-semibold text-gray-800 transition-colors hover:border-brand-600 hover:bg-brand-50 hover:text-brand-800 dark:border-white/14 dark:text-gray-100 dark:hover:border-brand-400 dark:hover:bg-brand-400/10 dark:hover:text-white"
                >
                  {plan.cta}
                </Link>
              </article>
            ))}
          </div>
          <p className="mt-6 text-sm text-gray-600 dark:text-gray-300">
            Need custom limits, dedicated infrastructure, or an on-premises deployment?{' '}
            <a href="mailto:sales@mindroom.chat" className="font-semibold text-gray-950 underline decoration-gray-400 underline-offset-2 hover:decoration-gray-950 dark:text-white dark:hover:decoration-white">
              Email sales@mindroom.chat
            </a>
          </p>
        </div>
      </section>

      <section className="bg-brand-950 py-20 text-white">
        <div className="mx-auto flex max-w-7xl flex-col gap-8 px-4 sm:px-6 lg:flex-row lg:items-center lg:justify-between lg:px-8">
          <div>
            <h2 className="text-4xl font-semibold tracking-[-0.03em] text-white sm:text-5xl">
              Run MindRoom <em className="font-serif text-[1.08em] font-normal italic tracking-normal text-brand-300">today.</em>
            </h2>
            <p className="mt-4 max-w-2xl text-base leading-7 text-brand-100/80">
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
