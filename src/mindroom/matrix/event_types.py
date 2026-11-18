"""Matrix event type constants shared across protocol layers."""

CALL_MEMBER_EVENT_TYPE = "org.matrix.msc3401.call.member"
CALL_ENCRYPTION_KEYS_EVENT_TYPE = "io.element.call.encryption_keys"
RTC_NOTIFICATION_EVENT_TYPE = "org.matrix.msc4075.rtc.notification"
# Each agent publishes its live background jobs as state keyed by its own user ID, for a client jobs bar.
BACKGROUND_JOBS_EVENT_TYPE = "io.mindroom.background_jobs"
# Each entity publishes its runtime's origin, whether it has a worker computer, and its role, keyed by its own user ID,
# so MindRoom Chat finds its MindRoom server without a typed URL and shows what the entity does.
MINDROOM_SERVER_EVENT_TYPE = "io.mindroom.server"
# The router publishes the room's open `!auto_approve` windows as state keyed by its own user ID, for a client indicator.
AUTO_APPROVE_WINDOWS_EVENT_TYPE = "io.mindroom.auto_approve"
# The router publishes the room's joined MindRoom accounts as state keyed by its own user ID, so a client recognizes
# agents whatever their user names.
MINDROOM_ACCOUNTS_EVENT_TYPE = "io.mindroom.accounts"
# The bot that previews a `!config set` change saves it as state keyed by the preview's event ID until it is confirmed.
PENDING_CONFIG_EVENT_TYPE = "com.mindroom.pending.config"
# Room admins set per-room MindRoom settings, one state event per setting keyed by its name, content `{"value": ...}`.
ROOM_SETTINGS_EVENT_TYPE = "io.mindroom.room_settings"
