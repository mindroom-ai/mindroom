import { useCallback, useEffect } from "react";

const DISCARD_MESSAGE = "Discard unsaved changes?";

// Editors whose unsaved draft lives in page state, which leaving the page drops.
let unsavedDraftCount = 0;

/** Asks before leaving the page drops an unsaved draft; true means go ahead. */
export function confirmLeavingUnsavedDrafts(): boolean {
  return unsavedDraftCount === 0 || window.confirm(DISCARD_MESSAGE);
}

/**
 * Registers an editor's unsaved draft, so leaving the page asks first, and
 * returns the check the editor runs before an action replaces the draft.
 */
export function useUnsavedDraft(isDirty: boolean): () => boolean {
  useEffect(() => {
    if (!isDirty) return;
    unsavedDraftCount += 1;
    return () => {
      unsavedDraftCount -= 1;
    };
  }, [isDirty]);
  return useCallback(
    () => !isDirty || window.confirm(DISCARD_MESSAGE),
    [isDirty],
  );
}
