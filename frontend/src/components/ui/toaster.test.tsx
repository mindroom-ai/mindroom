import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Toaster, toast } from "./toaster";

describe("Toaster", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    // Expire whatever a test left, so the next one starts with no toasts.
    act(() => {
      vi.runOnlyPendingTimers();
    });
    vi.useRealTimers();
  });

  it("shows a toast after an earlier one, and each expires 5 s after it appears", () => {
    render(<Toaster />);

    act(() => {
      toast({ title: "Saved" });
    });
    act(() => {
      vi.advanceTimersByTime(1000);
      toast({ title: "Save Failed", variant: "destructive" });
    });

    expect(screen.getByText("Saved")).toBeInTheDocument();
    expect(screen.getByText("Save Failed")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(4000);
    });

    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
    expect(screen.getByText("Save Failed")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(screen.queryByText("Save Failed")).not.toBeInTheDocument();
  });

  it("announces each new toast politely, without repeating the ones still shown", () => {
    render(<Toaster />);

    act(() => {
      toast({ title: "Credentials saved" });
      toast({ title: "Save failed", variant: "destructive" });
    });

    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    // A status region is atomic by default, which would re-read every shown toast on each new one.
    expect(region).toHaveAttribute("aria-atomic", "false");
    expect(region).toHaveTextContent("Credentials saved");
    expect(region).toHaveTextContent("Save failed");
  });

  it("closes only the toast whose X was clicked", () => {
    render(<Toaster />);

    act(() => {
      toast({ title: "First" });
      toast({ title: "Second" });
      toast({ title: "Third" });
    });
    fireEvent.click(
      screen.getAllByRole("button", { name: "Dismiss notification" })[1],
    );

    expect(screen.getByText("First")).toBeInTheDocument();
    expect(screen.queryByText("Second")).not.toBeInTheDocument();
    expect(screen.getByText("Third")).toBeInTheDocument();
  });
});
