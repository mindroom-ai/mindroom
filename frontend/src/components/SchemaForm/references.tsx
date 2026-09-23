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
  const { config } = useConfigStore();
  const extra = useContext(ExtraReferenceOptions);

  return useMemo(() => {
    const agents = config?.agents ?? {};
    const teams = config?.teams ?? {};
    const configured: ReferenceOptions = {
      model: Object.keys(config?.models ?? {}),
      agent: Object.keys(agents),
      room: [
        ...Object.keys(config?.rooms ?? {}),
        ...Object.values(agents).flatMap((agent) => agent.rooms ?? []),
        ...Object.values(teams).flatMap((team) => team.rooms ?? []),
      ],
      tool: [],
    };
    return Object.fromEntries(
      REFERENCE_KINDS.map((kind) => [
        kind,
        [...new Set([...configured[kind], ...(extra[kind] ?? [])])].sort(),
      ]),
    ) as ReferenceOptions;
  }, [config, extra]);
}
