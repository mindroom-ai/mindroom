# Fork Changes

## 2026-02-24

- Fix oversized edit events to keep `m.new_content.msgtype` as `m.text` (instead of `m.file`) while preserving `io.mindroom.long_text` sidecar metadata.
  This avoids Matrix clients showing edited long messages as broken/malformed after edit aggregation.
