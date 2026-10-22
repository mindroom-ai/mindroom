import type { ReactNode } from "react";
import { hostList, sentAsNote } from "./egressRowText";
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
        className={`w-full text-sm sm:min-w-[720px] ${STACKED.table}`}
      >
        <thead
          className={`text-left text-xs text-muted-foreground ${STACKED.head}`}
        >
          <tr>
            <th scope="col" className="w-[32%] px-5 py-3 font-medium">
              Service
            </th>
            <th scope="col" className="w-[22%] px-4 py-3 font-medium">
              Hosts
            </th>
            <th scope="col" className="w-[18%] px-4 py-3 font-medium">
              Status
            </th>
            <th scope="col" className="px-5 py-3 text-right font-medium">
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
    <tr className={`border-t border-border/60 ${STACKED.row}`}>
      <th
        colSpan={4}
        scope="colgroup"
        className={`bg-muted/20 px-5 py-2 text-left font-normal ${STACKED_CELL}`}
      >
        <h3 className="text-xs font-medium text-muted-foreground">{label}</h3>
      </th>
    </tr>
  );
}

/** The hosts a service's rules cover, and how its key is sent there. */
export function ServiceHosts({
  service,
}: {
  service: EgressCredentialService;
}) {
  const hosts = hostList(service);
  const sentAs = sentAsNote(service);
  return (
    <div className="space-y-0.5">
      <p className="break-words">{hosts ?? "—"}</p>
      {sentAs && <p className="text-xs text-muted-foreground">{sentAs}</p>}
    </div>
  );
}
