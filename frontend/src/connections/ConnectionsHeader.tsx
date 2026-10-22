import { type ReactNode, useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import mindroomLogo from "../../../assets/logo/logo-mark-animated.svgz?url";
import { requestConnection } from "./request";
import type { ConnectionsIdentity } from "./types";

type Page = "agents" | "keys";

const LINKS: readonly { page: Page; href: string; label: string }[] = [
  { page: "agents", href: "/connections", label: "Agents" },
  { page: "keys", href: "/connections/egress", label: "Agent Keys" },
];

/** What a non-administrator turned away from the dashboard is told on arriving, with `notice=admin-only`. */
const ADMIN_ONLY_NOTICE =
  "The dashboard is for administrators. Manage your agents' keys and accounts here.";

const dismissedKey = (id: string) => `connections-notice-dismissed:${id}`;

/** The notice the address asks for, unless it was dismissed in this browser session. */
function noticeFromAddress(): { id: string; text: string } | null {
  const id = new URLSearchParams(window.location.search).get("notice");
  if (id !== "admin-only") return null;
  try {
    if (window.sessionStorage.getItem(dismissedKey(id)) !== null) return null;
  } catch {
    // Storage can be off, in which case the notice is simply shown.
  }
  return { id, text: ADMIN_ONLY_NOTICE };
}

/**
 * Title, intro, who is signed in, and links between the Connections pages.
 * `howItWorks` is the longer explanation behind a disclosure under the intro.
 * A `notice` in the page's address, such as `admin-only` after a redirect,
 * shows its text above the page until it is dismissed for the session.
 *
 * The identity is a courtesy: when it cannot be loaded the header shows the
 * links without it, and the page's own requests report any real failure.
 * It also says whether the egress broker is on. The Agent Keys page exists
 * only then, so other pages link it only once the identity says so.
 */
export function ConnectionsHeader({
  current,
  title,
  howItWorks,
  children,
}: {
  current: Page;
  title: string;
  howItWorks?: ReactNode;
  children: ReactNode;
}) {
  const [identity, setIdentity] = useState<ConnectionsIdentity | null>(null);
  const [explaining, setExplaining] = useState(false);
  const [notice, setNotice] = useState(noticeFromAddress);
  const links = LINKS.filter(
    ({ page }) =>
      page !== "keys" || current === "keys" || identity?.egress_enabled,
  );

  useEffect(() => {
    const controller = new AbortController();
    requestConnection<ConnectionsIdentity>(
      "/api/connections/me",
      controller.signal,
    )
      .then((data) => {
        if (!controller.signal.aborted) setIdentity(data);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  // Tabs underline the page they are on, resting on the rule below them.
  const linkClass = (active: boolean) =>
    `-mb-px border-b-2 px-1 pb-3 pt-1 font-medium transition-colors focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
      active
        ? "border-primary text-foreground"
        : "border-transparent text-muted-foreground hover:border-border hover:text-foreground"
    }`;
  return (
    <header className="space-y-4">
      <div className="flex items-start gap-4">
        <img src={mindroomLogo} alt="" className="h-12 w-12 shrink-0" />
        <div className="space-y-1.5">
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
            {title}
          </h1>
          <p className="max-w-2xl text-sm text-muted-foreground">{children}</p>
          {howItWorks && (
            <div className="max-w-2xl space-y-1.5 text-sm text-muted-foreground">
              <button
                type="button"
                aria-expanded={explaining}
                aria-controls={explaining ? "how-this-works" : undefined}
                onClick={() => setExplaining(!explaining)}
                className="inline-flex items-center gap-1 rounded-md font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                How this works
                <ChevronDown
                  aria-hidden="true"
                  className={`h-3.5 w-3.5 transition-transform motion-reduce:transition-none ${explaining ? "rotate-180" : ""}`}
                />
              </button>
              {explaining && <p id="how-this-works">{howItWorks}</p>}
            </div>
          )}
        </div>
      </div>
      {notice && (
        // Told on arrival, not an error, so it is read politely.
        <Alert role="status">
          <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
            {notice.text}
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => {
                try {
                  window.sessionStorage.setItem(dismissedKey(notice.id), "1");
                } catch {
                  // Without storage it stays dismissed until the page is loaded again.
                }
                setNotice(null);
              }}
            >
              Dismiss
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {/* When the name does not fit beside the tabs it wraps above them, so the tabs stay on the rule. */}
      <div className="flex flex-wrap-reverse items-end justify-between gap-x-4 gap-y-1 border-b text-sm">
        <nav
          aria-label="Connections pages"
          className="flex flex-wrap items-end gap-5"
        >
          {links.map(({ page, href, label }) => (
            <a
              key={page}
              href={href}
              aria-current={page === current ? "page" : undefined}
              className={linkClass(page === current)}
            >
              {label}
            </a>
          ))}
          {identity?.is_administrator && (
            <a href="/" className={linkClass(false)}>
              Dashboard
            </a>
          )}
        </nav>
        {identity && (
          <span
            // The same box as a tab, so its text lines up with theirs.
            className="-mb-px break-all border-b-2 border-transparent pb-3 pt-1 text-muted-foreground"
            title={identity.matrix_user_id}
          >
            {`Signed in as ${identity.display_name}`}
          </span>
        )}
      </div>
    </header>
  );
}
