import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { getNavigationValue, NAV_ITEMS, Navigation } from "./Navigation";

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
  ["Settings", "/settings"],
] as const;

function CurrentPath() {
  return <output aria-label="Current path">{useLocation().pathname}</output>;
}

function HistoryControls() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      Go back
    </button>
  );
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
  it("defaults to dashboard for empty and unknown paths", () => {
    expect(getNavigationValue("/")).toBe("dashboard");
    expect(getNavigationValue("/unknown")).toBe("dashboard");
  });

  it("ignores trailing and repeated slashes for known tabs", () => {
    expect(getNavigationValue("/dashboard/")).toBe("dashboard");
    expect(getNavigationValue("///agents//")).toBe("agents");
    expect(getNavigationValue("/teams/details")).toBe("teams");
    expect(getNavigationValue("/usage")).toBe("usage");
  });

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

  it("closes the mobile dialog and restores focus after history navigation", async () => {
    render(
      <MemoryRouter initialEntries={["/agents", "/usage"]} initialIndex={1}>
        <Navigation mode="mobile" />
        <CurrentPath />
        <HistoryControls />
      </MemoryRouter>,
    );
    const trigger = screen.getByRole("button", { name: /open navigation/i });
    const backButton = screen.getByRole("button", { name: "Go back" });
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.click(backButton);

    expect(screen.getByLabelText("Current path")).toHaveTextContent("/agents");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
