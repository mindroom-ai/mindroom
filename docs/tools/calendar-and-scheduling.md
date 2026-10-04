---
icon: lucide/wrench
---

# Calendar & Scheduling

Use these tools to give an agent access to Google Calendar, Google Tasks, or Cal.com bookings, or to let it schedule its own future work in MindRoom.

- [`google_calendar`] - Read Google Calendar events and availability, and optionally create, update, or delete events.
- [`google_tasks`] - List, create, update, complete, and delete Google Tasks.
- [`cal_com`] - Query Cal.com availability and manage bookings.
- [`scheduler`](../scheduling.md#scheduler) - Schedule, edit, list, and cancel MindRoom tasks and reminders in the current conversation; documented on [Scheduling](../scheduling.md).

## Google Setup

`google_calendar` and `google_tasks` use Google OAuth instead of API keys, with separate `google_calendar` and `google_tasks` connections.
Connect Google with [Google Services OAuth For Local Installs](../deployment/google-services-user-oauth.md) or, for custom clients and scopes, [Google Services OAuth](../deployment/google-services-oauth.md).
An agent's `worker_scope` decides whose Google account it uses; see [Where Connections Are Stored](../oauth-framework.md#where-connections-are-stored).
To use a Google Workspace service account instead, see [Service Account](../deployment/google-services-oauth.md#service-account).
If an agent calls either tool before the account is connected, the tool returns a connect link instead of a result.

## [`google_calendar`]

<video controls playsinline preload="metadata" aria-label="Two people plan a weekend trip with an agent that checks the calendar and books it" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/4fea9538-5d22-43b4-9147-7e8b496400a8#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/ba40bcb7-19aa-4085-9c28-e99f1d171320#t=0.1" type="video/mp4">
</video>

`google_calendar` provides `list_events()`, `get_event()`, `fetch_all_events()`, `find_available_slots()`, `list_calendars()`, `check_availability()`, `get_event_attendees()`, `search_events()`, `create_event()`, `update_event()`, `delete_event()`, `quick_add_event()`, `move_event()`, and `respond_to_event()`.
`find_available_slots()` finds weekday openings around the user's timed events, within working hours inferred from their Google Calendar locale rather than their custom working-hours setting; all-day events do not block slots.
Dates without a time or offset, such as `2026-10-05`, are read as UTC, so the returned slots use UTC hours; convert them to the user's timezone before presenting them.
`list_calendars()` returns the IDs of the other calendars the connected account can use.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `calendar_id` | `text` | `no` | `primary` | Google Calendar ID to query or update. |
| `allow_update` | `boolean` | `no` | `true` | Expose `create_event()`, `update_event()`, `delete_event()`, `quick_add_event()`, `move_event()`, and `respond_to_event()`; set `false` for a read-only agent. |

```yaml
agents:
  assistant:
    tools:
      - google_calendar:
          calendar_id: primary
          allow_update: true
```

```python
list_events(limit=5)
find_available_slots(start_date="2026-04-01", end_date="2026-04-03", duration_minutes=30)
create_event(
    start_date="2026-04-02T15:00:00",
    end_date="2026-04-02T15:30:00",
    timezone="America/Los_Angeles",
    title="Deployment review",
    attendees=["ops@example.com"],
    notify_attendees=True,
    add_google_meet_link=True,
)
```

`create_event()` reads times without an offset in its `timezone` argument, which defaults to UTC rather than MindRoom's configured `timezone`, so pass the user's IANA timezone or include an offset.
`create_event()` emails invitations to attendees only when called with `notify_attendees=True`; by default guests are added without an email.

If the Google Calendar connection lacks any required Calendar permission, the tool stays unavailable until the user reconnects and grants it.

## [`google_tasks`]

`google_tasks` works with the connected user's Google Tasks lists.

| Function | Behavior |
| --- | --- |
| `google_tasks_list_task_lists()` | Returns up to 1,000 task lists with their IDs and titles. |
| `google_tasks_list_tasks()` | Returns up to 100 tasks per page from one list, including subtasks and tasks assigned to the user from Google Docs or Chat. Completed tasks, including those completed in Google's own apps, appear only with `show_completed=True`. |
| `google_tasks_create_task()` | Creates a task with optional notes, due date, and parent task. |
| `google_tasks_update_task()` | Changes only the fields you pass: title, notes, due date, or completion state. Pass an empty string as `notes` or `due` to remove that field. |
| `google_tasks_delete_task()` | Deletes one task. Deleting a task assigned from Google Docs or Chat also deletes the original assignment. |

Both list functions return a `nextPageToken` when more results remain.
Every function uses the user's default list, `@default`, when no `task_list_id` is given.
Due dates use `YYYY-MM-DD`, because Google Tasks stores a due day without a time.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `read_tasks` | `boolean` | `no` | `true` | Expose `google_tasks_list_task_lists()` and `google_tasks_list_tasks()`. |
| `manage_tasks` | `boolean` | `no` | `true` | Expose task creation, updates, completion, and deletion. |

```yaml
agents:
  assistant:
    tools:
      - google_tasks:
          read_tasks: true
          manage_tasks: true
```

```python
google_tasks_list_tasks()
google_tasks_create_task("Renew passport", notes="Bring two photos", due="2026-10-15")
google_tasks_update_task("dGFzay0x", completed=True)
```

## [`cal_com`]

`cal_com` uses the Cal.com v2 API at `https://api.cal.com/v2` to look up availability and manage bookings.
It provides `get_available_slots()`, `create_booking()`, `get_upcoming_bookings()`, `reschedule_booking()`, and `cancel_booking()`.
Slot lookup and new bookings use the configured `event_type_id`.
Returned times are converted from UTC into `user_timezone`.
Use the per-function `enable_*` flags when an agent should, for example, only check availability or only manage existing bookings.

| Option | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `api_key` | `password` | `no` | `null` | Cal.com API key; set it through the dashboard or credential store, not inline YAML. |
| `event_type_id` | `number` | `no` | `null` | Cal.com event type ID used for slot lookup and new bookings. |
| `user_timezone` | `text` | `no` | `null` | IANA timezone for returned booking times; `America/New_York` when unset. |
| `timeout` | `number` | `no` | `30` | Per-request HTTP timeout in seconds. |
| `enable_get_available_slots` | `boolean` | `no` | `true` | Enable `get_available_slots()`. |
| `enable_create_booking` | `boolean` | `no` | `true` | Enable `create_booking()`. |
| `enable_get_upcoming_bookings` | `boolean` | `no` | `true` | Enable `get_upcoming_bookings()`. |
| `enable_reschedule_booking` | `boolean` | `no` | `true` | Enable `reschedule_booking()`. |
| `enable_cancel_booking` | `boolean` | `no` | `true` | Enable `cancel_booking()`. |
| `all` | `boolean` | `no` | `false` | Enable every Cal.com function regardless of the `enable_*` flags. |

Every function needs `api_key`, and slot lookup and new bookings also need `event_type_id`; supply them as stored credentials or through the `CALCOM_API_KEY` and `CALCOM_EVENT_TYPE_ID` environment variables of the MindRoom process.

```yaml
agents:
  scheduler_assistant:
    tools:
      - cal_com:
          event_type_id: 123456
          user_timezone: America/Los_Angeles
          enable_cancel_booking: false
```

```python
get_available_slots(start_date="2026-04-01", end_date="2026-04-07")
create_booking(
    start_time="2026-04-03T17:00:00+00:00",
    name="Alex Example",
    email="alex@example.com",
)
get_upcoming_bookings(email="alex@example.com")
```

## Related Docs

- [Tools Overview](index.md)
- [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration)
- [Scheduling](../scheduling.md)
