"""Matrix event type constants shared across protocol layers."""

CALL_MEMBER_EVENT_TYPE = "org.matrix.msc3401.call.member"
CALL_ENCRYPTION_KEYS_EVENT_TYPE = "io.element.call.encryption_keys"
RTC_NOTIFICATION_EVENT_TYPE = "org.matrix.msc4075.rtc.notification"
# Each agent publishes its live background jobs as state keyed by its own user ID, for a client jobs bar.
BACKGROUND_JOBS_EVENT_TYPE = "io.mindroom.background_jobs"
# Each entity publishes its runtime's origin and whether it has a worker computer, keyed by its own user ID, so
# MindRoom Chat finds its MindRoom server without a typed URL.
MINDROOM_SERVER_EVENT_TYPE = "io.mindroom.server"
# The router publishes the room's open `!auto_approve` windows as state keyed by its own user ID, for a client indicator.
AUTO_APPROVE_WINDOWS_EVENT_TYPE = "io.mindroom.auto_approve"
