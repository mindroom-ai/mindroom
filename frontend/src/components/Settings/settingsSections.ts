import type { ConfigValidationIssue } from "@/lib/configValidation";
import {
  classifySchemaNode,
  objectProperties,
  rootPropertySchema,
  type JsonSchema,
} from "@/lib/configSchema";

/** One config root, optionally limited to some of its keys, in listed order. */
export interface SettingsEntry {
  root: string;
  keys?: readonly string[];
}

interface SettingsSection {
  id: string;
  title: string;
  description: string;
  entries: SettingsEntry[];
}

/**
 * Roots edited on dedicated dashboard pages.
 * "*" means the page owns the whole root; a key list means only those keys.
 */
const PAGE_OWNED_ROOTS: Record<string, "*" | readonly string[]> = {
  agents: "*",
  teams: "*",
  rooms: "*",
  room_models: "*",
  models: "*",
  memory: "*",
  knowledge_bases: "*",
  voice: "*",
  calls: "*",
  room_defaults: ["admins"],
};

const SETTINGS_SECTIONS: readonly SettingsSection[] = [
  {
    id: "responses",
    title: "Responses",
    description: "How agents format, stream, and deliver replies by default.",
    entries: [
      {
        root: "defaults",
        keys: [
          "markdown",
          "enable_streaming",
          "streaming",
          "large_message_strategy",
          "show_stop_button",
          "show_tool_calls",
          "coalescing",
          "auto_resume_after_restart",
        ],
      },
    ],
  },
  {
    id: "history",
    title: "History and context",
    description:
      "What agents replay, learn, and summarize from earlier conversation.",
    entries: [
      {
        root: "defaults",
        keys: [
          "learning",
          "learning_mode",
          "num_history_runs",
          "num_history_messages",
          "max_tool_calls_from_history",
          "compress_tool_results",
          "compaction",
          "max_preload_chars",
          "thread_summary_model",
          "thread_summary_temperature",
          "thread_summary_first_threshold",
          "thread_summary_subsequent_interval",
        ],
      },
      { root: "room_thread_summary_models" },
    ],
  },
  {
    id: "tools",
    title: "Tools and workers",
    description: "Default tools and where routed tool calls run.",
    entries: [
      {
        root: "defaults",
        keys: [
          "tools",
          "allow_self_config",
          "worker_tools",
          "worker_scope",
          "worker_grantable_credentials",
          "tool_output_auto_save_threshold_bytes",
        ],
      },
    ],
  },
  {
    id: "router",
    title: "Router",
    description:
      "How the router picks a responder and which invites it accepts.",
    entries: [{ root: "router" }],
  },
  {
    id: "personal-rooms",
    title: "Personal rooms",
    description: "One private room per person, owned by a selected agent.",
    entries: [{ root: "personal_rooms" }],
  },
  {
    id: "room-defaults",
    title: "Room defaults",
    description:
      "Matrix state every managed room inherits. Room admins are managed on the Rooms page.",
    entries: [
      {
        root: "room_defaults",
        keys: ["join_policy", "listed", "encrypted", "invite_users"],
      },
    ],
  },
  {
    id: "tool-approval",
    title: "Tool approval",
    description: "Which tool calls pause for a human decision.",
    entries: [{ root: "tool_approval" }],
  },
  {
    id: "prompts",
    title: "Prompts",
    description: "Overrides for built-in prompts.",
    entries: [{ root: "prompts" }],
  },
  {
    id: "mcp",
    title: "MCP servers",
    description: "Model Context Protocol servers whose tools agents can use.",
    entries: [{ root: "mcp_servers" }],
  },
  {
    id: "plugins",
    title: "Plugins",
    description: "Plugins that add tools, skills, and hooks.",
    entries: [{ root: "plugins" }],
  },
  {
    id: "access",
    title: "Access and identity",
    description:
      "Administrators, permissions, and accounts MindRoom treats specially.",
    entries: [
      { root: "administrators" },
      { root: "authorization" },
      { root: "bot_accounts" },
      { root: "mindroom_user" },
      { root: "external_trigger_policy" },
    ],
  },
  {
    id: "runtime",
    title: "Matrix and runtime",
    description: "Scheduling, Matrix sync, and event storage.",
    entries: [
      { root: "timezone" },
      { root: "scheduler_catch_up_grace_seconds" },
      { root: "matrix_space" },
      { root: "matrix_sync" },
      { root: "event_journal" },
    ],
  },
  {
    id: "diagnostics",
    title: "Diagnostics",
    description: "Troubleshooting output.",
    entries: [{ root: "debug" }],
  },
];

function rootObjectKeys(root: JsonSchema, key: string): string[] | null {
  const node = classifySchemaNode(rootPropertySchema(root, key), root);
  return node.kind === "object"
    ? objectProperties(node.schema, root).map(([property]) => property)
    : null;
}

/**
 * Sections for the roots this schema defines, plus an "Other" section with
 * every root or key no page or section claims, so new options are never hidden.
 */
export function resolveSettingsSections(root: JsonSchema): SettingsSection[] {
  const rootKeys = Object.keys(root.properties ?? {});
  const sections = SETTINGS_SECTIONS.map((section) => ({
    ...section,
    entries: section.entries
      .filter((entry) => rootKeys.includes(entry.root))
      .map((entry) => {
        if (entry.keys == null) {
          return entry;
        }
        const available = rootObjectKeys(root, entry.root) ?? [];
        return {
          ...entry,
          keys: entry.keys.filter((key) => available.includes(key)),
        };
      })
      .filter((entry) => entry.keys == null || entry.keys.length > 0),
  })).filter((section) => section.entries.length > 0);

  const listedEntries = SETTINGS_SECTIONS.flatMap((section) => section.entries);
  const other: SettingsEntry[] = [];
  for (const key of rootKeys) {
    const owned = PAGE_OWNED_ROOTS[key];
    const entries = listedEntries.filter((entry) => entry.root === key);
    if (owned === "*" || entries.some((entry) => entry.keys == null)) {
      continue;
    }
    const claimed = new Set([
      ...(owned ?? []),
      ...entries.flatMap((entry) => entry.keys ?? []),
    ]);
    if (claimed.size === 0) {
      other.push({ root: key });
      continue;
    }
    const remaining = (rootObjectKeys(root, key) ?? []).filter(
      (property) => !claimed.has(property),
    );
    if (remaining.length > 0) {
      other.push({ root: key, keys: remaining });
    }
  }
  if (other.length > 0) {
    sections.push({
      id: "other",
      title: "Other",
      description: "Settings without a dedicated section yet.",
      entries: other,
    });
  }
  return sections;
}

/** Whether any validation issue falls inside what this section renders. */
export function sectionHasIssue(
  section: SettingsSection,
  issues: ConfigValidationIssue[],
): boolean {
  return issues.some(({ loc }) =>
    section.entries.some(
      (entry) =>
        loc[0] === entry.root &&
        (entry.keys == null ||
          loc.length === 1 ||
          entry.keys.includes(String(loc[1]))),
    ),
  );
}
