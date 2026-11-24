import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FilterSelector } from "./FilterSelector";

const OPTIONS = [
  { value: "all", label: "Show All" },
  { value: "configured", label: "Configured", count: 3 },
];

describe("FilterSelector", () => {
  it("exposes the selected option as a pressed toggle", () => {
    const onChange = vi.fn();
    render(
      <FilterSelector options={OPTIONS} value="all" onChange={onChange} />,
    );

    expect(screen.getByRole("button", { name: "Show All" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const configured = screen.getByRole("button", { name: /^Configured/ });
    expect(configured).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(configured);
    expect(onChange).toHaveBeenCalledWith("configured");
  });
});
