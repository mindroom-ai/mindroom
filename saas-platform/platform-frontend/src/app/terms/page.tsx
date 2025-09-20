export default function TermsPage() {
  return (
    <main className="max-w-3xl px-6 py-12 mx-auto space-y-6">
      <header className="space-y-2">
        <p className="text-sm text-muted-foreground">Last updated: September 20, 2025</p>
        <h1 className="text-3xl font-semibold">MindRoom Terms of Service</h1>
        <p className="text-base text-muted-foreground">
          These terms outline how you may use the MindRoom platform preview environment. They are not a
          substitute for a signed customer agreement.
        </p>
      </header>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Acceptable Use</h2>
        <p>
          You agree to access MindRoom only for evaluation and development purposes. Do not use the
          service to store production data or personal information that you are not authorized to
          share.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Account Responsibility</h2>
        <p>
          Keep credentials secure and notify the MindRoom team if you suspect unauthorized access. You
          are responsible for actions taken using your account during this preview period.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Service Changes</h2>
        <p>
          MindRoom may change or suspend preview features at any time. We strive to notify testers, but
          changes can occur without prior notice while the platform is under active development.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Feedback</h2>
        <p>
          Sharing feedback helps improve MindRoom. By submitting suggestions you grant the MindRoom
          team permission to use them without obligation to you.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">Contact</h2>
        <p>
          Questions? Reach the team at <a className="text-primary" href="mailto:support@mindroom.chat">support@mindroom.chat</a>.
        </p>
      </section>
    </main>
  )
}
