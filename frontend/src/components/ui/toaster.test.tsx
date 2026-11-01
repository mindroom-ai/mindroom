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

  it("shows a toast after an earlier one, and both expire", () => {
    render(<Toaster />);

    act(() => {
      toast({ title: "Saved" });
    });
    act(() => {
      toast({ title: "Save Failed", variant: "destructive" });
    });

    expect(screen.getByText("Saved")).toBeInTheDocument();
    expect(screen.getByText("Save Failed")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(5000);
    });

    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
    expect(screen.queryByText("Save Failed")).not.toBeInTheDocument();
  });

  it("closes only the toast whose X was clicked", () => {
    render(<Toaster />);

    act(() => {
      toast({ title: "First" });
      toast({ title: "Second" });
    });
    fireEvent.click(screen.getAllByRole("button")[1]);

    expect(screen.getByText("First")).toBeInTheDocument();
    expect(screen.queryByText("Second")).not.toBeInTheDocument();
  });
});
