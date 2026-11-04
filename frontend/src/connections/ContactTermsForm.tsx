import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import { openInPage } from "./navigation";
import type { ContactAgent, ContactToolChoice } from "./types";

/** The rules and tools an owner is choosing for their side of one contact. */
export interface TermsDraft {
  rules: string;
  tools: string[];
}

/**
 * What a tool lets the other side reach, said where the owner chooses it.
 * Running code reaches everything, so it is the only note such a tool needs.
 */
export function ToolReach({
  choice,
  connected,
}: {
  choice: ContactToolChoice;
  /** Whether the agent's account for the tool is connected, `undefined` when unknown. */
  connected: boolean | undefined;
}) {
  const notes: string[] = [];
  if (choice.executes_code)
    notes.push(
      "Runs code: the other side can ask it to reach anything your agent's worker or process can.",
    );
  else if (choice.reaches_files)
    notes.push(
      "Reaches your agent's files: the other side can ask for what it can read and change what it can write, such as MEMORY.md.",
    );
  if (choice.provider) notes.push("Acts with your connected account.");
  return (
    <>
      {notes.map((note) => (
        <span key={note} className="block text-xs text-muted-foreground">
          {note}
        </span>
      ))}
      {choice.provider && connected === false && (
        <span className="block text-xs text-amber-700 dark:text-amber-400">
          Not connected, so contact turns cannot use it.{" "}
          <a
            href="/connections"
            onClick={openInPage}
            className="font-medium underline underline-offset-4"
          >
            Connect it
          </a>
        </span>
      )}
    </>
  );
}

/** The rules that always apply, which the owner can read but not change. */
export function ConfiguredRules({ rules }: { rules: string[] }) {
  if (rules.length === 0) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">
        Always apply, set by your administrator
      </p>
      <ul className="list-disc space-y-0.5 pl-5 text-sm">
        {rules.map((rule) => (
          <li key={rule}>{rule}</li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The owner's own rules and the tools a contact may use, for accepting a
 * request or changing a contact. `matrix_message` is always offered, so it is
 * shown but cannot be chosen.
 */
export function ContactTermsForm({
  agent,
  draft,
  connected,
  onChange,
}: {
  agent: ContactAgent;
  draft: TermsDraft;
  connected: Record<string, boolean> | undefined;
  onChange: (draft: TermsDraft) => void;
}) {
  const toggle = (name: string, chosen: boolean) =>
    onChange({
      ...draft,
      tools: chosen
        ? agent.tool_choices
            .map((choice) => choice.name)
            .filter((tool) => tool === name || draft.tools.includes(tool))
        : draft.tools.filter((tool) => tool !== name),
    });
  return (
    <div className="space-y-4">
      <ConfiguredRules rules={agent.configured_rules} />
      <div className="space-y-1.5">
        <label htmlFor="contact-rules" className="text-sm font-medium">
          Your rules
        </label>
        <Textarea
          id="contact-rules"
          value={draft.rules}
          placeholder="One rule per line, such as: Share only when I am free or busy."
          onChange={(event) =>
            onChange({ ...draft, rules: event.target.value })
          }
        />
      </div>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">
          Tools the other side can ask your agent to use
        </legend>
        {agent.tool_choices.map((choice) => {
          const id = `contact-tool-${choice.name}`;
          return (
            <div key={choice.name} className="flex items-start gap-2">
              <Checkbox
                id={id}
                className="mt-0.5"
                checked={draft.tools.includes(choice.name)}
                onCheckedChange={(checked) =>
                  toggle(choice.name, checked === true)
                }
              />
              <span className="min-w-0">
                <label htmlFor={id} className="block text-sm">
                  {choice.display_name}
                </label>
                <ToolReach
                  choice={choice}
                  connected={
                    choice.provider ? connected?.[choice.provider] : undefined
                  }
                />
              </span>
            </div>
          );
        })}
        <div className="flex items-start gap-2">
          <Checkbox
            id="contact-tool-matrix_message"
            className="mt-0.5"
            checked
            disabled
          />
          <span className="min-w-0">
            <label
              htmlFor="contact-tool-matrix_message"
              className="block text-sm"
            >
              Matrix Message
            </label>
            <span className="block text-xs text-muted-foreground">
              Always offered, so your agent can answer.
            </span>
          </span>
        </div>
      </fieldset>
    </div>
  );
}
