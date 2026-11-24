import { useEffect, useRef } from "react";
import { SHARED_ACCOUNT_TITLE, sharedAccountBody } from "./accountCopy";
import { ConfirmDialog } from "./ConfirmDialog";

/** Asked before connecting an account on a shared agent, since everyone who uses the agent acts as it. */
export function SharedAccountDialog({
  open,
  agent,
  provider,
  onContinue,
  onCancel,
}: {
  open: boolean;
  agent: string;
  provider: string;
  onContinue: () => void;
  onCancel: () => void;
}) {
  // The dialog may stay on screen a moment after Continue, so a second click must not go on again.
  const continued = useRef(false);
  useEffect(() => {
    if (open) continued.current = false;
  }, [open]);
  const doContinue = () => {
    if (continued.current) return;
    continued.current = true;
    onContinue();
  };
  return (
    <ConfirmDialog
      open={open}
      title={SHARED_ACCOUNT_TITLE}
      description={sharedAccountBody(agent, provider)}
      confirmLabel="Continue"
      destructive={false}
      onCancel={onCancel}
      onConfirm={doContinue}
    />
  );
}
