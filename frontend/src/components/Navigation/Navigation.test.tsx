import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { NAV_ITEMS, Navigation } from "./Navigation";

const EXPECTED_ROUTES = [
  ["Dashboard", "/dashboard"],
  ["Agents", "/agents"],
  ["Teams", "/teams"],
  ["Rooms", "/rooms"],
  ["Schedules", "/schedules"],
  ["External", "/unconfigured-rooms"],
  ["Models", "/models"],
  ["Usage", "/usage"],
  ["Memory", "/memory"],
  ["Knowledge", "/knowledge"],
  ["Credentials", "/credentials"],
  ["Voice", "/voice"],
  ["Tools", "/integrations"],
  ["Skills", "/skills"],
] as const;

function CurrentPath() {
  return <output aria-label="Current path">{useLocation().pathname}</output>;
}

function renderNavigation(initialPath = "/dashboard") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Navigation />
      <CurrentPath />
    </MemoryRouter>,
  );
}

describe("Navigation", () => {
  it("uses accessible links and marks the current route", () => {
    renderNavigation("/agents");

    const navigation = screen.getByRole("navigation", {
      name: "Primary navigation",
    });
    const agentsLink = within(navigation).getByRole("link", {
      name: "Agents",
    });

    expect(agentsLink).toHaveAttribute("href", "/agents");
    expect(agentsLink).toHaveAttribute("aria-current", "page");

    fireEvent.click(within(navigation).getByRole("link", { name: "Models" }));

    expect(screen.getByLabelText("Current path")).toHaveTextContent("/models");
    expect(
      within(navigation).getByRole("link", { name: "Models" }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("keeps every application route reachable", () => {
    renderNavigation();

    expect(NAV_ITEMS.map(({ label, value }) => [label, `/${value}`])).toEqual(
      EXPECTED_ROUTES,
    );
    const navigation = screen.getByRole("navigation", {
      name: "Primary navigation",
    });
    for (const [label, href] of EXPECTED_ROUTES) {
      expect(
        within(navigation).getByRole("link", { name: label }),
      ).toHaveAttribute("href", href);
    }
  });

  it("closes the mobile navigation dialog after following a link", () => {
    renderNavigation();

    fireEvent.click(screen.getByRole("button", { name: /open navigation/i }));
    const dialog = screen.getByRole("dialog");

    fireEvent.click(within(dialog).getByRole("link", { name: "Usage" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Current path")).toHaveTextContent("/usage");
  });
});
