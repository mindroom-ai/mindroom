import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ConfirmDialog } from "./ConfirmDialog";
import {
  ALWAYS_OFFERED,
  ConfiguredRules,
  ContactTermsForm,
  ruleLines,
  ToolReach,
  type TermsDraft,
} from "./ContactTermsForm";
import type {
  ContactAgent,
  ContactPartyView,
  ContactTerms,
  ContactView,
} from "./types";

export type ContactAction = "accept" | "decline" | "update" | "remove";

type Dialog = ContactAction | null;

const WORKING: Record<ContactAction, string> = {
  accept: "Accepting…",
  decline: "Declining…",
  update: "Saving…",
  remove: "Removing…",
};

function DateValue({ value }: { value: string }) {
  const date = new Date(value);
  return <time dateTime={date.toISOString()}>{date.toLocaleDateString()}</time>;
}

/** Who a party is, naming an agent's owner, since the other side chooses its agent's name. */
function partyLine(party: ContactPartyView): string {
  return party.kind === "agent"
    ? `${party.id}, the agent of ${party.owner}`
    : `${party.id}, in person`;
}

/** The tools an owner can choose, as the contact offers them now. */
function chosenTools(contact: ContactView): string[] {
  return (contact.effective_tools ?? []).filter(
    (tool) => tool !== "matrix_message",
  );
}

/** Only what the owner changed, so an unchanged field keeps what is set. */
function changedTerms(contact: ContactView, draft: TermsDraft): ContactTerms {
  const terms: ContactTerms = {};
  if (draft.rules !== (contact.rules ?? "")) terms.rules = draft.rules;
  const before = chosenTools(contact);
  if (
    draft.tools.length !== before.length ||
    draft.tools.some((tool) => !before.includes(tool))
  )
    terms.tools = draft.tools;
  return terms;
}

function statusLine(contact: ContactView): string | null {
  const owner = contact.other.owner;
  if (contact.status === "pending" && contact.side === "requester")
    return `Waiting for ${owner} to answer.`;
  if (contact.status === "declined")
    return contact.side === "target"
      ? "You declined. Remove it to let either of you ask again."
      : `${owner} declined. They can remove it to let you ask again.`;
  return null;
}

/** Your side's rules, tools, and judge, as a contact turn uses them now. */
function YourTerms({
  contact,
  agent,
  connected,
}: {
  contact: ContactView;
  agent: ContactAgent | undefined;
  connected: Record<string, boolean> | undefined;
}) {
  if (contact.you.kind === "person")
    return (
      <p className="text-sm text-muted-foreground">
        You talk in the contact room yourself, so no rules or tools apply to
        your side.
      </p>
    );
  if (!agent || contact.effective_tools === null)
    return (
      <p className="text-sm text-muted-foreground">
        Contacts are off for {contact.you.display_name}, so its rules and tools
        cannot be shown or changed.
      </p>
    );
  const choices = new Map(
    agent.tool_choices.map((choice) => [choice.name, choice]),
  );
  const ownRules = ruleLines(contact.rules ?? "");
  return (
    <div className="grid gap-4 text-sm sm:grid-cols-2">
      <div className="space-y-2">
        <h4 className="font-medium">Rules</h4>
        <ConfiguredRules rules={agent.configured_rules} />
        {ownRules.length > 0 ? (
          <div className="space-y-1">
            <p className="text-xs font-medium text-muted-foreground">
              Yours for this contact
            </p>
            <ul className="list-disc space-y-0.5 pl-5">
              {ownRules.map((rule, index) => (
                <li key={index}>{rule}</li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="text-muted-foreground">
            You set no rules of your own for this contact.
          </p>
        )}
        <p className="text-muted-foreground">
          {agent.judged
            ? "A judge checks each message against these rules before it is sent."
            : "No judge checks messages; the rules only guide your agent."}
        </p>
      </div>
      <div className="space-y-2">
        <h4 className="font-medium">Tools</h4>
        <ul className="space-y-1.5">
          {chosenTools(contact).map((tool) => {
            const choice = choices.get(tool);
            return (
              <li key={tool}>
                <span className="block">{choice?.display_name ?? tool}</span>
                {choice && (
                  <ToolReach
                    choice={choice}
                    connected={
                      choice.provider ? connected?.[choice.provider] : undefined
                    }
                  />
                )}
              </li>
            );
          })}
          <li>
            <span className="block">Matrix Message</span>
            <span className="block text-xs text-muted-foreground">
              {ALWAYS_OFFERED}
            </span>
          </li>
        </ul>
      </div>
    </div>
  );
}

/**
 * One contact with the actions its owner can take: accept or decline a
 * request to them, change their side's terms, withdraw a request they sent,
 * and remove a contact or a request they declined.
 */
export function ContactCard({
  contact,
  agent,
  connected,
  busy,
  working,
  onAction,
}: {
  contact: ContactView;
  agent: ContactAgent | undefined;
  connected: Record<string, boolean> | undefined;
  /** A change to any contact is running, so none can start. */
  busy: boolean;
  /** The change to this contact that is running. */
  working: ContactAction | null;
  onAction: (action: ContactAction, terms?: ContactTerms) => void;
}) {
  const [dialog, setDialog] = useState<Dialog>(null);
  const [draft, setDraft] = useState<TermsDraft>({ rules: "", tools: [] });
  const titleId = `contact-${contact.id}`;
  const editable =
    contact.you.kind === "agent" &&
    agent !== undefined &&
    contact.effective_tools !== null;
  const open = (next: Dialog) => {
    setDraft({ rules: contact.rules ?? "", tools: chosenTools(contact) });
    setDialog(next);
  };
  const confirm = (action: ContactAction) => {
    setDialog(null);
    if (action === "accept" || action === "update") {
      const terms = changedTerms(contact, draft);
      if (action === "update" && Object.keys(terms).length === 0) return;
      onAction(action, terms);
    } else onAction(action);
  };
  const pending = contact.status === "pending";
  const askedYou = pending && contact.side === "target";
  // An agent whose contacts are off cannot open a contact room, so its request can only be declined.
  const canAccept = askedYou && (contact.you.kind === "person" || editable);
  const canRemove =
    contact.status === "active" ||
    (pending && contact.side === "requester") ||
    (contact.status === "declined" && contact.side === "target");
  const status = statusLine(contact);
  const removeCopy =
    contact.status === "active"
      ? {
          title: "Remove this contact?",
          description:
            "The agents in this contact leave its room. You keep the room and its history.",
          label: "Remove…",
        }
      : pending
        ? {
            title: "Withdraw this request?",
            description: `${contact.other.owner} is no longer asked.`,
            label: "Withdraw…",
          }
        : {
            title: "Remove the declined request?",
            description: "Either of you can then ask again.",
            label: "Remove…",
          };
  const form = agent && (
    <ContactTermsForm
      agent={agent}
      draft={draft}
      connected={connected}
      onChange={setDraft}
    />
  );
  return (
    <Card role="article" aria-labelledby={titleId}>
      <CardContent className="space-y-4 pt-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <h3 id={titleId} className="break-words text-lg font-semibold">
              {contact.other.display_name}
            </h3>
            <p className="break-all text-sm text-muted-foreground">
              {partyLine(contact.other)}
            </p>
            <p className="text-sm">
              {contact.you.kind === "agent"
                ? `With your agent ${contact.you.display_name}`
                : "With you in person"}
              {contact.requested_at && (
                <>
                  {" · Asked "}
                  <DateValue value={contact.requested_at} />
                </>
              )}
              {contact.decided_at && (
                <>
                  {contact.status === "active"
                    ? " · Active since "
                    : " · Declined "}
                  <DateValue value={contact.decided_at} />
                </>
              )}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {contact.room_id && (
              <Button asChild variant="outline" size="sm">
                <a
                  href={`https://matrix.to/#/${encodeURIComponent(contact.room_id)}`}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  Open room
                </a>
              </Button>
            )}
            {canAccept && (
              <Button size="sm" disabled={busy} onClick={() => open("accept")}>
                Accept…
              </Button>
            )}
            {askedYou && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => open("decline")}
              >
                Decline…
              </Button>
            )}
            {editable && !askedYou && contact.status !== "declined" && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => open("update")}
              >
                Edit…
              </Button>
            )}
            {canRemove && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => open("remove")}
              >
                {removeCopy.label}
              </Button>
            )}
          </div>
        </div>
        <blockquote className="border-l-2 pl-3 text-sm italic text-muted-foreground">
          {contact.reason}
        </blockquote>
        {working && (
          <p role="status" className="text-sm text-muted-foreground">
            {WORKING[working]}
          </p>
        )}
        {status && <p className="text-sm">{status}</p>}
        {contact.status !== "declined" && (
          <YourTerms contact={contact} agent={agent} connected={connected} />
        )}
      </CardContent>
      <ConfirmDialog
        open={dialog === "accept"}
        title={`Accept ${contact.other.display_name}?`}
        description={
          contact.you.kind === "agent"
            ? `${contact.you.display_name} and ${contact.other.display_name} get a shared room to talk in. The other side can start turns of your agent there, with your authority and the tools you choose below.`
            : `You and ${contact.other.display_name} get a shared room to talk in.`
        }
        confirmLabel="Accept"
        destructive={false}
        onCancel={() => setDialog(null)}
        onConfirm={() => confirm("accept")}
      >
        {contact.you.kind === "agent" && form}
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog === "update"}
        title="Change your rules and tools"
        description="Changes apply from your agent's next turn in this contact."
        confirmLabel="Save"
        destructive={false}
        onCancel={() => setDialog(null)}
        onConfirm={() => confirm("update")}
      >
        {form}
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog === "decline"}
        title="Decline this request?"
        description="Neither of you can ask again until you remove the declined request."
        confirmLabel="Decline"
        onCancel={() => setDialog(null)}
        onConfirm={() => confirm("decline")}
      />
      <ConfirmDialog
        open={dialog === "remove"}
        title={removeCopy.title}
        description={removeCopy.description}
        confirmLabel={removeCopy.label.replace("…", "")}
        onCancel={() => setDialog(null)}
        onConfirm={() => confirm("remove")}
      />
    </Card>
  );
}
