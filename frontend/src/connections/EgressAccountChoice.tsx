import type { ReactNode } from "react";
import { Code } from "./EgressServiceTable";

/**
 * Which account a service of the user's own signs in with: the one the agent's
 * tools use, or another account of the same login that only this service uses
 * (`oauth_separate_account`), which requests pick by the service's header.
 */
export function EgressAccountChoice({
  id,
  provider,
  name,
  separateAccount,
  toolsAddress,
  usedBy,
  disconnects,
  signsIn,
  disabled,
  onChange,
}: {
  id: string;
  /** The login's display name, such as Gmail. */
  provider: string;
  /** The service's name, which its header carries. */
  name: string;
  separateAccount: boolean;
  /** The address of the account the tools use, where another service shows it. */
  toolsAddress: string | null;
  /** The display name of another service of the agent that uses the tools' account. */
  usedBy: string | null;
  /** The address of the service's own connected account that saving as chosen removes. */
  disconnects: string | null;
  /** Saving with another account signs in to it next: the service has none of its own yet. */
  signsIn: boolean;
  disabled: boolean;
  onChange: (separateAccount: boolean) => void;
}) {
  const choice = (own: boolean, label: string, note: ReactNode) => (
    <div>
      <label className="flex items-start gap-2 text-sm">
        <input
          type="radio"
          name={id}
          className="mt-0.5 h-4 w-4"
          checked={separateAccount === own}
          disabled={disabled}
          aria-describedby={note ? `${id}-${own}` : undefined}
          onChange={() => onChange(own)}
        />
        {label}
      </label>
      {note && (
        <p id={`${id}-${own}`} className="pl-6 text-xs text-muted-foreground">
          {note}
        </p>
      )}
    </div>
  );
  return (
    <div
      role="radiogroup"
      aria-labelledby={`${id}-label`}
      className="max-w-md space-y-1"
    >
      <p id={`${id}-label`} className="text-xs font-medium">
        Account
      </p>
      {choice(
        false,
        `The account your tools use${toolsAddress ? ` (${toolsAddress})` : ""}`,
        usedBy &&
          toolsAddress &&
          `The ${usedBy} row already uses this account.`,
      )}
      {choice(
        true,
        `Another ${provider} account`,
        <>
          {signsIn ? "You sign in right after saving. " : ""}Agents choose it
          with <Code>X-Agent-Keys-Service: {name || "<name>"}</Code>
        </>,
      )}
      {disconnects && (
        <p className="text-xs text-muted-foreground">
          Saving disconnects {disconnects}.
        </p>
      )}
    </div>
  );
}
