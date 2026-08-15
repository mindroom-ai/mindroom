export default function PrivacyPage() {
  return (
    <main className="max-w-3xl px-6 py-12 mx-auto space-y-6">
      <header className="space-y-2">
        <p className="text-sm text-muted-foreground">Last reviewed: August 14, 2026</p>
        <h1 className="text-3xl font-semibold">MindRoom Privacy Notice</h1>
        <p className="text-base text-muted-foreground">
          This hosted control-plane notice supplements the canonical MindRoom privacy policy.
        </p>
      </header>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Information We Collect</h2>
        <p>
          We process account profile and status data, subscriptions and payments, hosted instance records,
          usage metrics, audit events, and consent choices needed to operate the service.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">How Information Is Used</h2>
        <p>
          This data supports service operation, billing, security, fraud prevention, compliance, and the
          preferences you select.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Retention</h2>
        <p>
          Hosted cleanup schedules soft-deleted application accounts for removal after a seven-day grace
          period, ordinary audit logs after 90 days, and usage metrics after 365 days. Some security and
          deletion audit events are excluded from ordinary audit cleanup.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Third Parties</h2>
        <p>
          The hosted control plane uses Supabase for database and authentication services and Stripe for
          payment processing. Matrix homeserver and installation operators have their own data boundaries.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Contact</h2>
        <p>
          Read the full <a className="text-primary" href="https://docs.mindroom.chat/privacy/">MindRoom privacy policy</a>.
          For private privacy, account, or data requests, email <a className="text-primary" href="mailto:support@mindroom.chat">support@mindroom.chat</a>.
          For general policy questions, use <a className="text-primary" href="https://github.com/mindroom-ai/mindroom/issues">MindRoom GitHub issues</a> without posting personal data or private account details.
        </p>
      </section>
    </main>
  )
}
