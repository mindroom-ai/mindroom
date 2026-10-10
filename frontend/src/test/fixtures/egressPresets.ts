import type { EgressPreset } from "@/connections/types";

/** The presets as `GET /api/connections/egress/presets` returns them (src/mindroom/egress_broker/presets.py). */
export const EGRESS_PRESETS_FIXTURE: EgressPreset[] = [
  {
    id: "github",
    display_name: "GitHub",
    description: "GitHub API, gh CLI, and git over HTTPS",
    oauth_provider: "github",
    rules: [
      {
        host: "api.github.com",
        port: null,
        path_prefix: "/",
      },
      {
        host: "uploads.github.com",
        port: null,
        path_prefix: "/",
      },
      {
        host: "github.com",
        port: null,
        path_prefix: "/",
      },
    ],
    placeholder_env: {
      GH_TOKEN: "mindroom-brokered",
      GITHUB_TOKEN: "mindroom-brokered",
    },
  },
  {
    id: "google_drive",
    display_name: "Google Drive",
    description: "Google Drive API",
    oauth_provider: "google_drive",
    rules: [
      {
        host: "www.googleapis.com",
        port: null,
        path_prefix: "/drive/",
      },
      {
        host: "www.googleapis.com",
        port: null,
        path_prefix: "/upload/drive/",
      },
    ],
    placeholder_env: {},
  },
  {
    id: "google_gmail",
    display_name: "Gmail",
    description: "Gmail API",
    oauth_provider: "google_gmail",
    rules: [
      {
        host: "gmail.googleapis.com",
        port: null,
        path_prefix: "/",
      },
      {
        host: "www.googleapis.com",
        port: null,
        path_prefix: "/gmail/",
      },
    ],
    placeholder_env: {},
  },
  {
    id: "google_calendar",
    display_name: "Google Calendar",
    description: "Google Calendar API",
    oauth_provider: "google_calendar",
    rules: [
      {
        host: "www.googleapis.com",
        port: null,
        path_prefix: "/calendar/",
      },
    ],
    placeholder_env: {},
  },
  {
    id: "google_sheets",
    display_name: "Google Sheets",
    description: "Google Sheets API",
    oauth_provider: "google_sheets",
    rules: [
      {
        host: "sheets.googleapis.com",
        port: null,
        path_prefix: "/",
      },
    ],
    placeholder_env: {},
  },
  {
    id: "google_docs",
    display_name: "Google Docs",
    description: "Google Docs API",
    oauth_provider: "google_docs",
    rules: [
      {
        host: "docs.googleapis.com",
        port: null,
        path_prefix: "/",
      },
    ],
    placeholder_env: {},
  },
  {
    id: "google_tasks",
    display_name: "Google Tasks",
    description: "Google Tasks API",
    oauth_provider: "google_tasks",
    rules: [
      {
        host: "tasks.googleapis.com",
        port: null,
        path_prefix: "/",
      },
      {
        host: "www.googleapis.com",
        port: null,
        path_prefix: "/tasks/",
      },
    ],
    placeholder_env: {},
  },
  {
    id: "atlassian",
    display_name: "Atlassian",
    description: "Atlassian Cloud APIs (Jira and Confluence)",
    oauth_provider: "atlassian",
    rules: [
      {
        host: "api.atlassian.com",
        port: null,
        path_prefix: "/",
      },
    ],
    placeholder_env: {},
  },
  {
    id: "openai",
    display_name: "OpenAI",
    description: "OpenAI API",
    oauth_provider: null,
    rules: [
      {
        host: "api.openai.com",
        port: null,
        path_prefix: "/",
      },
    ],
    placeholder_env: {
      OPENAI_API_KEY: "mindroom-brokered",
    },
  },
  {
    id: "anthropic",
    display_name: "Anthropic",
    description: "Anthropic API",
    oauth_provider: null,
    rules: [
      {
        host: "api.anthropic.com",
        port: null,
        path_prefix: "/",
      },
    ],
    placeholder_env: {
      ANTHROPIC_API_KEY: "mindroom-brokered",
    },
  },
];
