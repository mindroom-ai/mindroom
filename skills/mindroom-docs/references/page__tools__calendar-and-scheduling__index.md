# Calendar & Scheduling

Use these tools to read external calendars, manage bookings, and schedule future agent work inside MindRoom.

## What This Page Covers

This page documents the built-in tools in the `calendar-and-scheduling` group.
Use these tools when you need Google Calendar access, Cal.com booking APIs, or Matrix-native scheduled tasks that post back into MindRoom later.

## Tools On This Page

- \[`google_calendar`\] - Read Google Calendar data and, when enabled, create, update, or delete events through Google OAuth.
- \[`cal_com`\] - Query Cal.com availability and manage bookings through the Cal.com API.
- \[`scheduler`\] - Schedule, edit, list, and cancel MindRoom tasks and reminders in the current Matrix conversation.

## Common Setup Notes

`google_calendar` is a Google-backed OAuth integration.
It uses the `google_calendar` OAuth provider instead of an API key form.
It can use scoped OAuth credentials for isolated worker scopes. Use [Google Services OAuth (Admin Setup)](https://docs.mindroom.chat/deployment/google-services-oauth/index.md) or [Google Services OAuth (Individual Setup)](https://docs.mindroom.chat/deployment/google-services-user-oauth/index.md) to connect Google before enabling `google_calendar`. `cal_com` is a standard credential-backed tool with its own config fields and no shared-only restriction.
`scheduler` is MindRoom's built-in scheduling system, so it does not need dashboard OAuth setup or API keys.
Unlike the two calendar API tools, `scheduler` depends on the active Matrix `ToolRuntimeContext`, so it only works from a live room or thread.
MindRoom also includes `scheduler` in `defaults.tools` by default on this branch.

## \[`google_calendar`\]

`google_calendar` wraps Agno's Google Calendar toolkit with MindRoom-scoped Google OAuth credentials.

### What It Does

`google_calendar` exposes `list_events()`, `fetch_all_events()`, `find_available_slots()`, `list_calendars()`, `create_event()`, `update_event()`, and `delete_event()`.
MindRoom loads the connected Google account from its unified credential store instead of relying on a per-process `token.json`.
The OAuth provider requests Google Calendar scope once, while MindRoom gates write methods with the `allow_update` setting.
Write calls are still part of the tool surface, but they are only exposed when `allow_update: true` is configured.
`find_available_slots()` derives openings from the user's current calendar events plus working-hours settings inferred from Google Calendar settings and locale.

### Configuration

| Option         | Type      | Required | Default   | Notes                                                                     |
| -------------- | --------- | -------- | --------- | ------------------------------------------------------------------------- |
| `calendar_id`  | `text`    | `no`     | `primary` | Google Calendar ID to query or update.                                    |
| `allow_update` | `boolean` | `no`     | `false`   | Request write-capable behavior for create, update, and delete operations. |

### Example

```
agents:
  assistant:
    worker_scope: shared
    tools:
      - google_calendar:
          calendar_id: primary
          allow_update: true
```

```
list_events(limit=5)
find_available_slots(start_date="2026-04-01", end_date="2026-04-03", duration_minutes=30)
create_event(
    start_date="2026-04-02T15:00:00",
    end_date="2026-04-02T15:30:00",
    title="Deployment review",
    attendees=["ops@example.com"],
    add_google_meet_link=True,
)
```

### Notes

- `calendar_id` defaults to `primary`, and `list_calendars()` can return the other calendar IDs available to the connected account.
- If the Google Calendar connection is missing the required calendar scope, `google_calendar` stays unavailable until the user reconnects and grants it.
- Use the Google Services OAuth guides for consent-screen setup, redirect URIs, and environment variables.

## \[`cal_com`\]

`cal_com` talks to the Cal.com v2 booking API for availability lookup and booking management.

### What It Does

`cal_com` exposes `get_available_slots()`, `create_booking()`, `get_upcoming_bookings()`, `reschedule_booking()`, and `cancel_booking()`.
The toolkit uses one configured `event_type_id` as the default booking type for slot lookup and booking creation.
Responses are converted from UTC into `user_timezone` before they are returned.
The per-method enable flags let you narrow the exposed call surface when an agent should only inspect availability or only manage existing bookings.

### Configuration

| Option                         | Type       | Required | Default | Notes                                                                                              |
| ------------------------------ | ---------- | -------- | ------- | -------------------------------------------------------------------------------------------------- |
| `api_key`                      | `password` | `no`     | `null`  | Cal.com API key. Configure this through the dashboard or credential store rather than inline YAML. |
| `event_type_id`                | `number`   | `no`     | `null`  | Default Cal.com event type ID used for slot lookup and new bookings.                               |
| `user_timezone`                | `text`     | `no`     | `null`  | IANA timezone used when formatting returned booking times.                                         |
| `enable_get_available_slots`   | `boolean`  | `no`     | `true`  | Enable `get_available_slots()`.                                                                    |
| `enable_create_booking`        | `boolean`  | `no`     | `true`  | Enable `create_booking()`.                                                                         |
| `enable_get_upcoming_bookings` | `boolean`  | `no`     | `true`  | Enable `get_upcoming_bookings()`.                                                                  |
| `enable_reschedule_booking`    | `boolean`  | `no`     | `true`  | Enable `reschedule_booking()`.                                                                     |
| `enable_cancel_booking`        | `boolean`  | `no`     | `true`  | Enable `cancel_booking()`.                                                                         |
| `all`                          | `boolean`  | `no`     | `false` | Enable every Cal.com operation at once.                                                            |

### Example

```
agents:
  scheduler_assistant:
    tools:
      - cal_com:
          event_type_id: 123456
          user_timezone: America/Los_Angeles
          enable_cancel_booking: false
```

```
get_available_slots(start_date="2026-04-01", end_date="2026-04-07")
create_booking(
    start_time="2026-04-03T17:00:00+00:00",
    name="Alex Example",
    email="alex@example.com",
)
get_upcoming_bookings(email="alex@example.com")
```

### Notes

- Although the metadata marks `api_key` and `event_type_id` as optional fields, the runtime only works properly when those values are supplied either through stored credentials or the `CALCOM_API_KEY` and `CALCOM_EVENT_TYPE_ID` environment variables.
- If `user_timezone` is omitted, the upstream toolkit falls back to `America/New_York`.
- `api_key` is a password field, so MindRoom blocks inline YAML overrides for it in normal authored config.
- All current requests go to `https://api.cal.com/v2`.

## \[`scheduler`\]

`scheduler` is MindRoom's built-in task scheduler for future messages, reminders, and recurring agent work.

### What It Does

`scheduler` exposes `schedule()`, `edit_schedule()`, `list_schedules()`, and `cancel_schedule()`.
It reuses the same backend as `!schedule`, `!edit_schedule`, `!list_schedules`, and `!cancel_schedule`.
By default `schedule()` posts back into the current room or thread scope, while `new_thread=True` schedules a future room-level root message.
Scheduled tasks are stored in Matrix room state and persist across restarts.
The scheduler validates mentioned agents against the current room or thread before it saves a task.
If no Matrix room context is available, the tool returns an unavailable error instead of creating a task.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```
agents:
  assistant:
    tools:
      - scheduler
```

```
schedule("tomorrow at 9am @ops check the deployment")
schedule("every weekday at 8am post the on-call handoff summary", new_thread=True)
list_schedules()
edit_schedule("a1b2c3d4", "tomorrow at 10am @ops check the deployment")
cancel_schedule("a1b2c3d4")
```

### Notes

- `scheduler` needs no dashboard setup and is included in `defaults.tools` by default unless you explicitly disable that inheritance.
- Editing preserves the original schedule type, so switching between one-time and recurring schedules requires cancelling the old task and creating a new one.
- Conditional phrases such as `if` and `when` are converted into recurring polling schedules rather than real event subscriptions.
- Use [Scheduling](https://docs.mindroom.chat/scheduling/index.md) for the full command syntax, timezone behavior, persistence details, and command-line aliases.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Scheduling](https://docs.mindroom.chat/scheduling/index.md)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration)
- [Google Services OAuth (Admin Setup)](https://docs.mindroom.chat/deployment/google-services-oauth/index.md)
- [Google Services OAuth (Individual Setup)](https://docs.mindroom.chat/deployment/google-services-user-oauth/index.md)
