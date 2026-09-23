import { createContext, useContext, useMemo, type ReactNode } from "react";

import type { ReferenceKind, ReferenceOptions } from "@/lib/configSchema";
import { useConfigStore } from "@/store/configStore";

const REFERENCE_KINDS: ReferenceKind[] = ["model", "agent", "room", "tool"];

const ExtraReferenceOptions = createContext<Partial<ReferenceOptions>>({});

/** Add entity names the draft config does not list, such as the tool catalog. */
export function ReferenceOptionsProvider({
  options,
  children,
}: {
  options: Partial<ReferenceOptions>;
  children: ReactNode;
}) {
  return (
    <ExtraReferenceOptions.Provider value={options}>
      {children}
    </ExtraReferenceOptions.Provider>
  );
}

export function useReferenceOptions(): ReferenceOptions {
  // Agents and rooms come from the draft collections, which hold unsaved edits.
  const { config, agents, rooms } = useConfigStore();
  const extra = useContext(ExtraReferenceOptions);
  const models = config?.models;

  return useMemo(() => {
    const configured: ReferenceOptions = {
      model: Object.keys(models ?? {}),
      agent: agents.map((agent) => agent.id),
      room: rooms.map((room) => room.id),
      tool: [],
    };
    return Object.fromEntries(
      REFERENCE_KINDS.map((kind) => [
        kind,
        [...new Set([...configured[kind], ...(extra[kind] ?? [])])].sort(),
      ]),
    ) as ReferenceOptions;
  }, [models, agents, rooms, extra]);
}
