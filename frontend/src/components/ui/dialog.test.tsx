import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Dialog, DialogContent, DialogTitle } from "./dialog";

/** A dialog opened from a plain button, as cards open their settings, with no DialogTrigger. */
function CardWithDialog() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Configure</button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>Settings</DialogTitle>
          <button onClick={() => setOpen(false)}>Cancel</button>
        </DialogContent>
      </Dialog>
    </>
  );
}

describe("DialogContent", () => {
  it.each([
    [
      "Escape",
      () => fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" }),
    ],
    [
      "Cancel",
      () => fireEvent.click(screen.getByRole("button", { name: "Cancel" })),
    ],
  ])(
    "gives focus back to the button that opened it when %s closes it",
    async (_, close) => {
      render(<CardWithDialog />);
      const opener = screen.getByRole("button", { name: "Configure" });
      opener.focus();
      fireEvent.click(opener);
      await waitFor(() => expect(opener).not.toHaveFocus());

      close();

      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      await waitFor(() => expect(opener).toHaveFocus());
    },
  );
});
