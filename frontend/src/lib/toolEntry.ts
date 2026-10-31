export type ToolOverrides = Record<string, unknown>;

type NamedToolEntry = {
  name: string;
  overrides?: ToolOverrides | null;
  defer?: boolean;
  initial?: boolean;
};

/**
 * One authored `tools` entry: a bare name, the compact `{name: overrides}`
 * mapping the backend serializes, or the explicit named form. Entries are
 * immutable values; edits return new entries.
 */
export type ToolEntry =
  string | NamedToolEntry | Record<string, ToolOverrides | null>;

function isOverrideMapping(value: unknown): value is ToolOverrides {
  return value != null && typeof value === "object" && !Array.isArray(value);
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
      ...(isOverrideMapping(named.overrides) ? named.overrides : {}),
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
    overrides: isOverrideMapping(overrides) ? overrides : null,
  };
}

function sanitizeOverrideValue(value: unknown): unknown {
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

export function toolEntryName(entry: ToolEntry): string {
  return parseToolEntry(entry).name;
}

export function toolNames(entries: ToolEntry[]): string[] {
  return entries.map(toolEntryName);
}

/** The entries for `names`, keeping each listed tool's entry and adding new names bare. */
export function toolEntriesForNames(
  names: string[],
  entries: ToolEntry[],
): ToolEntry[] {
  const entriesByName = new Map(
    entries.map((entry) => [toolEntryName(entry), entry]),
  );
  return names.map((name) => entriesByName.get(name) ?? name);
}

export function getToolOverrides(
  toolName: string,
  entries: ToolEntry[] | undefined,
): ToolOverrides | null {
  const matchingEntry = entries?.find(
    (entry) => toolEntryName(entry) === toolName,
  );
  return matchingEntry == null ? null : parseToolEntry(matchingEntry).overrides;
}

/**
 * Apply a merge patch to the named tool's overrides: a null or blank value
 * removes its key, and keys the patch does not name stay as authored. A tool
 * the entries do not list is not added.
 */
export function setToolOverridesInEntries(
  toolName: string,
  patch: ToolOverrides,
  entries: ToolEntry[],
): ToolEntry[] {
  return entries.map((entry) => {
    const parsed = parseToolEntry(entry);
    if (parsed.name !== toolName) {
      return entry;
    }
    const overrides = { ...parsed.overrides };
    for (const [key, value] of Object.entries(patch)) {
      const sanitized = sanitizeOverrideValue(value);
      if (sanitized === null) {
        delete overrides[key];
      } else {
        overrides[key] = sanitized;
      }
    }
    return Object.keys(overrides).length > 0
      ? { [toolName]: overrides }
      : toolName;
  });
}
