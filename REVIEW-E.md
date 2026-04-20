APPROVE

The revised test now pins the real bug by driving both the reporter and `wait_for_ready()` off a controlled monotonic clock at the production 1.0s poll cadence and asserting that `cold_start` is emitted around the 1.5s grace threshold before `ensure_worker()` can return.
