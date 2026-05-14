# ISSUE-211 Living Report

## Minimal Restart Decision

The restart intentionally fixes only model prompt ambiguity for Matrix reaction selections.
Matrix reactions are already event-targeted, so the implementation preserves that target and enriches the selection prompt with the original question context.
The restart explicitly does not add tombstones, numeric text reply routing changes, direct reply target parsing, or cross-process consume redesign.

## Implementation Notes

Active interactive question records now persist `question_text` and option label metadata alongside the existing key-to-value map.
Older persisted records continue to load by defaulting missing question text to an empty string and missing labels to an empty map.
`InteractiveSelection` now carries the question event id, question text, selected key, selected label, selected value, and thread id.
`build_selection_prompt(selection)` centralizes the prompt sent to the model and anchors the selection to the original `question_event_id` and `question_text` using JSON payload text rather than markdown fences.
Registration call sites pass the richer metadata from parsed interactive responses through normal response delivery and Matrix message tools.
