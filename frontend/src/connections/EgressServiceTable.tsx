import { Fragment, type ReactNode, useRef } from "react";
import { MoreHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { breakAfterUnderscores } from "./breakAfterUnderscores";
import { serviceHosts } from "./egressRowText";
import { BROKERED_PLACEHOLDER } from "./egressServiceForm";
import { STACKED, STACKED_CELL, STACKED_LABEL } from "./stackedTable";
import type { EgressCredentialService } from "./types";

/** The cells of one service row, in the table's column order. */
export const SERVICE_CELL = {
  name: `px-5 py-3 text-left font-normal ${STACKED_CELL} max-sm:px-4 max-sm:py-1`,
  hosts: `px-4 py-3 ${STACKED_CELL} ${STACKED_LABEL} max-sm:py-1`,
  status: `px-4 py-3 ${STACKED_CELL} ${STACKED_LABEL} max-sm:py-1`,
  actions: `px-5 py-3 text-right ${STACKED_CELL} max-sm:px-4 max-sm:py-2 max-sm:text-left`,
} as const;

export const SERVICE_ROW = `border-t border-border/60 ${STACKED.row} max-sm:py-2`;

/** A table of an agent's services; its children are the row groups, such as `EgressServiceRows`. */
export function EgressServiceTable({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="overflow-x-auto">
      <table
        aria-label={label}
        // Fixed widths, so a long description or button cannot starve the status column.
        className={`w-full text-sm sm:min-w-[720px] sm:table-fixed ${STACKED.table}`}
      >
        <thead
          className={`text-left text-xs text-muted-foreground ${STACKED.head}`}
        >
          <tr>
            <th scope="col" className="px-5 py-3 font-medium">
              Service
            </th>
            <th scope="col" className="w-[20%] px-4 py-3 font-medium">
              Hosts
            </th>
            <th scope="col" className="w-[26%] px-4 py-3 font-medium">
              Status
            </th>
            <th scope="col" className="w-56 px-5 py-3 text-right font-medium">
              Actions
            </th>
          </tr>
        </thead>
        {children}
      </table>
    </div>
  );
}

/** The heading row that opens a group of service rows. */
export function ServiceGroupHeading({ label }: { label: string }) {
  return (
    <tr className={STACKED.row}>
      <th
        colSpan={4}
        scope="rowgroup"
        className={`px-5 pb-2 pt-5 text-left font-normal ${STACKED_CELL} max-sm:px-4`}
      >
        <h3 className="text-xs font-semibold text-muted-foreground">{label}</h3>
      </th>
    </tr>
  );
}

/** A host, variable, or header name, set apart as code; it wraps only when wider than its cell. */
function Code({ children }: { children: ReactNode }) {
  return (
    <code className="inline-block max-w-full break-words rounded bg-muted px-1 py-px font-mono text-[0.95em] text-foreground">
      {children}
    </code>
  );
}

/** "a", "a and b", or "a, b, and c", as `formatList` joins strings, for items that are not plain text. */
function listOf(items: { key: string; node: ReactNode }[]): ReactNode {
  return items.map(({ key, node }, index) => (
    <Fragment key={key}>
      {index === 0
        ? null
        : index < items.length - 1
          ? ", "
          : items.length > 2
            ? ", and "
            : " and "}
      {node}
    </Fragment>
  ));
}

const SENT_AS: Record<
  string,
  (name: string | null | undefined) => { key: string; node: ReactNode }
> = {
  bearer: () => ({ key: "bearer", node: "as a bearer token" }),
  header: (name) => ({
    key: `header:${name ?? ""}`,
    node: name ? (
      <>
        as header <Code>{name}</Code>
      </>
    ) : (
      "as a header"
    ),
  }),
  query: (name) => ({
    key: `query:${name ?? ""}`,
    node: name ? (
      <>
        as query parameter <Code>{name}</Code>
      </>
    ) : (
      "as a query parameter"
    ),
  }),
  basic: () => ({ key: "basic", node: "with basic auth" }),
  template: () => ({ key: "template", node: "with a custom header template" }),
};

/**
 * How the service's rules send the key: "Sent as a bearer token", "Sent as
 * header X-Api-Key", and so on, once for each way its rules differ.
 */
function SentAs({ service }: { service: EgressCredentialService }) {
  const ways = new Map<string, ReactNode>();
  for (const rule of service.rules) {
    const way = rule.auth_type && SENT_AS[rule.auth_type]?.(rule.auth_name);
    if (way && !ways.has(way.key)) ways.set(way.key, way.node);
  }
  if (ways.size === 0) return null;
  return (
    <p className="text-xs text-muted-foreground">
      Sent {listOf([...ways].map(([key, node]) => ({ key, node })))}
    </p>
  );
}

/** The hosts a service's rules cover, and how its key is sent there. */
export function ServiceHosts({
  service,
}: {
  service: EgressCredentialService;
}) {
  const hosts = serviceHosts(service);
  return (
    <div className="space-y-1">
      <p className="break-words font-mono text-xs">
        {hosts.length > 0 ? hosts.join(", ") : "—"}
      </p>
      <SentAs service={service} />
    </div>
  );
}

/**
 * What the sandbox is told about the service: the variables it sets, or for a
 * service with a key and nothing to set, that the agent must be told which
 * website to use. Nothing where the listing does not name the variables, and
 * for an account service, which the agent finds through the API it calls.
 */
export function AgentSees({ service }: { service: EgressCredentialService }) {
  const names = service.placeholder_names;
  if (names === undefined || service.restrict_only) return null;
  const hosts = serviceHosts(service);
  if (names.length === 0 && (service.oauth !== null || hosts.length === 0))
    return null;
  return (
    <p className="break-words text-xs text-muted-foreground">
      {names.length > 0 ? (
        <>
          Your agent sees{" "}
          {names.map((name, index) => (
            <Fragment key={name}>
              {index > 0 && ", "}
              <Code>
                {breakAfterUnderscores(name)}=<wbr />
                <span className="whitespace-nowrap">
                  {BROKERED_PLACEHOLDER}
                </span>
              </Code>
            </Fragment>
          ))}
        </>
      ) : (
        <>
          Your agent is not told this key exists; mention{" "}
          {listOf(
            hosts.map((host) => ({ key: host, node: <Code>{host}</Code> })),
          )}{" "}
          in your request.
        </>
      )}
    </p>
  );
}

/** One action of a row's menu. */
export interface RowAction {
  label: string;
  /** Names the service too, as every row has the same actions. */
  ariaLabel: string;
  onSelect: () => void;
  disabled?: boolean;
  /** Removes something; these come last, apart from the rest. */
  destructive?: boolean;
}

/** A row's actions besides its main one, behind a ⋯ button; nothing without any. */
export function RowActionsMenu({
  service,
  actions,
  disabled,
}: {
  service: string;
  actions: RowAction[];
  disabled?: boolean;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  if (actions.length === 0) return null;
  const item = (action: RowAction) => (
    <DropdownMenuItem
      key={action.label}
      aria-label={action.ariaLabel}
      disabled={action.disabled}
      onSelect={() => {
        // A dialog the action opens returns focus to what had it, and the item is gone by then.
        trigger.current?.focus();
        action.onSelect();
      }}
      className={
        action.destructive
          ? "text-destructive focus:text-destructive"
          : undefined
      }
    >
      {action.label}
    </DropdownMenuItem>
  );
  const others = actions.filter((action) => !action.destructive);
  const destructive = actions.filter((action) => action.destructive);
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button
          ref={trigger}
          size="sm"
          variant="ghost"
          className="h-8 w-8 p-0"
          disabled={disabled}
          aria-label={`More actions for ${service}`}
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        // Leave focus where the chosen action moved it, such as a key field or a dialog.
        onCloseAutoFocus={(event) => {
          if (
            document.activeElement &&
            document.activeElement !== document.body
          )
            event.preventDefault();
        }}
      >
        {others.map(item)}
        {others.length > 0 && destructive.length > 0 && (
          <DropdownMenuSeparator />
        )}
        {destructive.map(item)}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
