import { type ComponentProps, useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./dialog";

/**
 * A dialog opened from a plain button, as cards open their settings, with no
 * DialogTrigger; most dashboard dialogs are also removed from the page on close.
 */
function CardWithDialog({
  onCloseAutoFocus,
  unmountOnClose = false,
}: Pick<ComponentProps<typeof DialogContent>, "onCloseAutoFocus"> & {
  unmountOnClose?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <input aria-label="Search" />
      <button onClick={() => setOpen(true)}>Configure</button>
      {(open || !unmountOnClose) && (
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogContent onCloseAutoFocus={onCloseAutoFocus}>
            <DialogTitle>Settings</DialogTitle>
            <DialogDescription>Tool settings.</DialogDescription>
            <button onClick={() => setOpen(false)}>Cancel</button>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

/** Focuses the Configure button and opens the dialog from it, which moves focus inside. */
async function openDialog(): Promise<HTMLElement> {
  const opener = screen.getByRole("button", { name: "Configure" });
  opener.focus();
  fireEvent.click(opener);
  await waitFor(() => expect(opener).not.toHaveFocus());
  return opener;
}

const pressEscape = () =>
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

describe("DialogContent", () => {
  it.each([
    { how: "Escape closes it", unmountOnClose: false, close: pressEscape },
    {
      how: "Cancel closes it",
      unmountOnClose: false,
      close: () =>
        fireEvent.click(screen.getByRole("button", { name: "Cancel" })),
    },
    {
      how: "Escape closes it and the page removes it",
      unmountOnClose: true,
      close: pressEscape,
    },
  ])(
    "gives focus back to the button that opened it when $how",
    async ({ unmountOnClose, close }) => {
      render(<CardWithDialog unmountOnClose={unmountOnClose} />);
      const opener = await openDialog();

      close();

      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      await waitFor(() => expect(opener).toHaveFocus());
    },
  );

  it("leaves focus where the caller's own close handler put it", async () => {
    render(
      <CardWithDialog
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          screen.getByRole("textbox", { name: "Search" }).focus();
        }}
      />,
    );
    await openDialog();

    pressEscape();

    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Search" })).toHaveFocus(),
    );
  });
});
