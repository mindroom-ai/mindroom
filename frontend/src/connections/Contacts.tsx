import { useEffect, useRef, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardContent } from "@/components/ui/card";
import { ConnectionsHeader } from "./ConnectionsHeader";
import { ContactCard, type ContactAction } from "./ContactCard";
import { errorText, requestConnection } from "./request";
import type {
  AgentState,
  ContactTerms,
  ContactView,
  ContactsPage,
} from "./types";

const contactsPath = "/api/connections/contacts";

const SECTIONS: readonly {
  title: string;
  holds: (contact: ContactView) => boolean;
}[] = [
  {
    title: "Waiting for you",
    holds: (contact) =>
      contact.status === "pending" && contact.side === "target",
  },
  { title: "Active", holds: (contact) => contact.status === "active" },
  {
    title: "Sent",
    holds: (contact) =>
      contact.status === "pending" && contact.side === "requester",
  },
  { title: "Declined", holds: (contact) => contact.status === "declined" },
];

/**
 * Whether each of the owner's agents has its accounts connected, by provider,
 * so a contact tool that needs an account the owner has not connected is
 * flagged. An agent whose state cannot be loaded is simply unknown.
 */
function useConnectedAccounts(page: ContactsPage | null) {
  const [connected, setConnected] = useState<
    Record<string, Record<string, boolean>>
  >({});
  useEffect(() => {
    if (!page) return;
    const controller = new AbortController();
    for (const { agent_name } of page.agents)
      void requestConnection<AgentState>(
        `/api/connections/agents/${encodeURIComponent(agent_name)}/state`,
        controller.signal,
      )
        .then((state) => {
          if (!controller.signal.aborted)
            setConnected((known) => ({
              ...known,
              [agent_name]: state.connected,
            }));
        })
        .catch(() => undefined);
    return () => controller.abort();
  }, [page]);
  return connected;
}

export function Contacts() {
  const [page, setPage] = useState<ContactsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const operation = useRef<AbortController | null>(null);
  const connected = useConnectedAccounts(page);

  useEffect(() => {
    const controller = new AbortController();
    void requestConnection<ContactsPage>(contactsPath, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setPage(data);
      })
      .catch((cause) => {
        if (!controller.signal.aborted)
          setError(
            errorText(
              cause,
              "Could not load your contacts. Reload this page to try again.",
            ),
          );
      });
    return () => {
      controller.abort();
      operation.current?.abort();
    };
  }, []);

  const act = async (
    contact: ContactView,
    action: ContactAction,
    terms: ContactTerms = {},
  ) => {
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setBusy(contact.id);
    setError(null);
    try {
      const next = await requestConnection<ContactsPage>(
        `${contactsPath}/${encodeURIComponent(contact.id)}/${action}`,
        controller.signal,
        "POST",
        terms,
      );
      if (!controller.signal.aborted) setPage(next);
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(errorText(cause, "Could not change this contact. Try again."));
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  };

  const agents = new Map(
    page?.agents.map((agent) => [agent.agent_name, agent]) ?? [],
  );
  return (
    <main className="min-h-screen bg-muted/20 px-4 py-8 sm:px-6 sm:py-12">
      <div className="mx-auto max-w-6xl space-y-6">
        <ConnectionsHeader
          current="contacts"
          title="Your contacts"
          howItWorks="A contact lets the other side start turns of your agent that run with your authority, limited to the tools you choose for that contact. Anything those tools can reach, the other side can ask for. Rules and the judge reduce accidental sharing, but they are not a security boundary."
        >
          See who your agents are in touch with, and the rules and tools they
          use there.
        </ConnectionsHeader>
        {error && (
          <Alert variant="destructive" role="alert">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {!page && !error && <p role="status">Loading your contacts…</p>}
        {page && page.contacts.length === 0 && (
          <Card>
            <CardContent className="pt-6 text-muted-foreground">
              No contacts yet. Ask your agent in your personal room to get in
              touch with someone.
            </CardContent>
          </Card>
        )}
        {page &&
          SECTIONS.map(({ title, holds }) => {
            const contacts = page.contacts.filter(holds);
            if (contacts.length === 0) return null;
            const headingId = `contacts-${title.toLowerCase().replace(/ /g, "-")}`;
            return (
              <section
                key={title}
                aria-labelledby={headingId}
                className="space-y-3"
              >
                <h2
                  id={headingId}
                  className="text-lg font-semibold tracking-tight"
                >
                  {title}
                </h2>
                {contacts.map((contact) => (
                  <ContactCard
                    key={contact.id}
                    contact={contact}
                    agent={
                      contact.you.kind === "agent"
                        ? agents.get(contact.you.id)
                        : undefined
                    }
                    connected={
                      contact.you.kind === "agent"
                        ? connected[contact.you.id]
                        : undefined
                    }
                    busy={busy !== null}
                    onAction={(action, terms) =>
                      void act(contact, action, terms)
                    }
                  />
                ))}
              </section>
            );
          })}
      </div>
    </main>
  );
}
