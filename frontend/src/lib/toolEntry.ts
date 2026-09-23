export type ToolOverrides = Record<string, unknown>;

type NamedToolEntry = {
  name: string;
  overrides?: ToolOverrides | null;
  defer?: boolean;
  initial?: boolean;
};

export type ToolEntry =
  string | NamedToolEntry | Record<string, ToolOverrides | null>;

function cloneOverrideValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return [...value];
  }
  return value;
}

function cloneOverrides(
  overrides: ToolOverrides | null | undefined,
): ToolOverrides {
  if (overrides == null) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(overrides).map(([key, value]) => [
      key,
      cloneOverrideValue(value),
    ]),
  );
}

function parseToolEntry(entry: ToolEntry): {
  name: string;
  overrides: ToolOverrides | null;
} {
  if (typeof entry === "string") {
    return { name: entry, overrides: null };
  }

  if ("name" in entry && typeof entry.name === "string") {
    const named = entry as NamedToolEntry;
    // The explicit form keeps lazy-loading flags beside its overrides; the
    // compact form this module writes stores them inside the mapping.
    const overrides = {
      ...(named.overrides != null &&
      typeof named.overrides === "object" &&
      !Array.isArray(named.overrides)
        ? cloneOverrides(named.overrides)
        : {}),
      ...(named.defer === true ? { defer: true } : {}),
      ...(named.initial === true ? { initial: true } : {}),
    };
    return {
      name: named.name,
      overrides: Object.keys(overrides).length > 0 ? overrides : null,
    };
  }

  const [name, overrides] = Object.entries(entry)[0] ?? [];
  if (typeof name !== "string") {
    throw new Error("Structured tool entries must include a tool name");
  }
  return {
    name,
    overrides:
      overrides != null &&
      typeof overrides === "object" &&
      !Array.isArray(overrides)
        ? cloneOverrides(overrides)
        : null,
  };
}

function makeStructuredToolEntry(
  toolName: string,
  overrides: ToolOverrides,
): ToolEntry {
  return { [toolName]: cloneOverrides(overrides) };
}

function sanitizeOverrideValue(value: unknown): unknown | null {
  if (value == null) {
    return null;
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
  }
  if (Array.isArray(value)) {
    const values = value
      .filter((entry): entry is string => typeof entry === "string")
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);
    return values.length > 0 ? values : null;
  }
  return value;
}

function sanitizeOverrides(
  overrides: ToolOverrides | null | undefined,
): ToolOverrides {
  if (overrides == null) {
    return {};
  }

  const sanitized: ToolOverrides = {};
  for (const [key, value] of Object.entries(overrides)) {
    const sanitizedValue = sanitizeOverrideValue(value);
    if (sanitizedValue !== null) {
      sanitized[key] = sanitizedValue;
    }
  }
  return sanitized;
}

export function cloneToolEntries(
  rawEntries: ToolEntry[] | null | undefined,
): ToolEntry[] {
  if (!rawEntries) {
    return [];
  }
  return rawEntries.map((entry) => {
    const parsed = parseToolEntry(entry);
    if (parsed.overrides == null) {
      return parsed.name;
    }
    return makeStructuredToolEntry(parsed.name, parsed.overrides);
  });
}

export function extractToolName(entry: ToolEntry): string {
  return parseToolEntry(entry).name;
}

export function normalizeToolEntries(
  rawEntries: ToolEntry[] | null | undefined,
): string[] {
  if (!rawEntries) {
    return [];
  }
  return rawEntries.map(extractToolName);
}

export function rebuildToolEntries(
  toolNames: string[],
  rawEntries: ToolEntry[] | null | undefined,
): ToolEntry[] {
  const rawEntriesByName = new Map(
    cloneToolEntries(rawEntries).map((entry) => {
      const parsed = parseToolEntry(entry);
      return [
        parsed.name,
        parsed.overrides == null
          ? parsed.name
          : makeStructuredToolEntry(parsed.name, parsed.overrides),
      ];
    }),
  );

  return toolNames.map(
    (toolName) => rawEntriesByName.get(toolName) ?? toolName,
  );
}

export function getToolOverrides(
  toolName: string,
  rawEntries: ToolEntry[] | null | undefined,
): ToolOverrides | null {
  if (!rawEntries) {
    return null;
  }
  const matchingEntry = rawEntries.find(
    (entry) => extractToolName(entry) === toolName,
  );
  if (!matchingEntry) {
    return null;
  }
  return parseToolEntry(matchingEntry).overrides;
}

export function setToolOverridesInEntries(
  toolName: string,
  overrides: ToolOverrides | null,
  rawEntries: ToolEntry[] | null | undefined,
): ToolEntry[] {
  const nextEntries = cloneToolEntries(rawEntries);
  const existingOverrides = getToolOverrides(toolName, nextEntries) ?? {};
  const mergedOverrides = sanitizeOverrides({
    ...existingOverrides,
    ...(overrides ?? {}),
  });
  const replacementEntry =
    Object.keys(mergedOverrides).length > 0
      ? makeStructuredToolEntry(toolName, mergedOverrides)
      : toolName;
  const existingIndex = nextEntries.findIndex(
    (entry) => extractToolName(entry) === toolName,
  );

  if (existingIndex >= 0) {
    nextEntries[existingIndex] = replacementEntry;
    return nextEntries;
  }

  if (Object.keys(mergedOverrides).length === 0) {
    return nextEntries;
  }

  return [...nextEntries, replacementEntry];
}
