export default function PrivacyPage() {
  return (
    <main className="max-w-3xl px-6 py-12 mx-auto space-y-6">
      <header className="space-y-2">
        <p className="text-sm text-muted-foreground">Last updated: September 20, 2025</p>
        <h1 className="text-3xl font-semibold">MindRoom Privacy Notice</h1>
        <p className="text-base text-muted-foreground">
          This preview environment is intended for evaluation and internal testing. The details below
          describe how we handle data while the platform is in staged rollout.
        </p>
      </header>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Information We Collect</h2>
        <p>
          We store account details, workspace configuration, and interaction logs needed to operate the
          Matrix-based agent runtime. Do not upload end-user personal data or regulated content during
          testing.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">How Information Is Used</h2>
        <p>
          Data collected in staging supports troubleshooting, product analytics, and improving agent
          quality. Access is limited to the MindRoom engineering team.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Retention</h2>
        <p>
          Logs and configuration snapshots may be reset or purged at any time. If you need staging data
          removed sooner, contact the team and we will wipe it promptly.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Third Parties</h2>
        <p>
          The preview stack uses Supabase for authentication and Stripe in test mode for billing flows.
          These services receive only the minimum data required for evaluation.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Contact</h2>
        <p>
          Reach us at <a className="text-primary" href="mailto:support@mindroom.chat">support@mindroom.chat</a> for privacy questions or data requests.
        </p>
      </section>
    </main>
  )
}
