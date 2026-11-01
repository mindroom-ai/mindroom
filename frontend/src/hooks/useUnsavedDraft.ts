import { useCallback, useEffect } from "react";

const DISCARD_MESSAGE = "Discard unsaved changes?";

// Editors whose unsaved draft lives in page state, which leaving the page drops.
let unsavedDraftCount = 0;

/** Asks before leaving the page drops an unsaved draft; true means go ahead. */
export function confirmLeavingUnsavedDrafts(): boolean {
  return unsavedDraftCount === 0 || window.confirm(DISCARD_MESSAGE);
}

/**
 * Makes reloading, closing the tab, or following a full-page link show the
 * browser's leave-page prompt while `hasUnsavedChanges` holds. The listener
 * exists only meanwhile, so the browser can keep the page in its back/forward cache.
 */
export function useBeforeUnloadPrompt(hasUnsavedChanges: boolean): void {
  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const askBeforeUnload = (event: BeforeUnloadEvent) =>
      event.preventDefault();
    window.addEventListener("beforeunload", askBeforeUnload);
    return () => window.removeEventListener("beforeunload", askBeforeUnload);
  }, [hasUnsavedChanges]);
}

/**
 * Registers an editor's unsaved draft, so leaving the page asks first, and
 * returns the check the editor runs before an action replaces the draft.
 */
export function useUnsavedDraft(isDirty: boolean): () => boolean {
  useBeforeUnloadPrompt(isDirty);
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
