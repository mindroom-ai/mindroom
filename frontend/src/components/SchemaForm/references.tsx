import { createContext, useContext, useMemo, type ReactNode } from "react";

import type { ReferenceKind, ReferenceOptions } from "@/lib/configSchema";
import { useConfigStore } from "@/store/configStore";

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
    const merge = (kind: ReferenceKind, names: string[]) =>
      [...new Set([...names, ...(extra[kind] ?? [])])].sort();
    return {
      model: merge("model", Object.keys(models ?? {})),
      agent: merge(
        "agent",
        agents.map((agent) => agent.id),
      ),
      room: merge(
        "room",
        rooms.map((room) => room.id),
      ),
      tool: merge("tool", []),
    };
  }, [models, agents, rooms, extra]);
}
