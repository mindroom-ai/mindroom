import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, type Mock } from "vitest";
import { UnconfiguredRooms } from "./UnconfiguredRooms";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("UnconfiguredRooms", () => {
  it("distinguishes and selects rooms with identical display names", async () => {
    const rooms = ["!first:example.com", "!second:example.com"];
    (global.fetch as Mock).mockResolvedValueOnce(
      jsonResponse({
        agents: [
          {
            agent_id: "team",
            display_name: "Team",
            configured_rooms: [],
            joined_rooms: rooms,
            unconfigured_rooms: rooms,
            unconfigured_room_details: rooms.map((room_id) => ({
              room_id,
              name: "Lobby",
            })),
          },
        ],
      }),
    );
    const user = userEvent.setup();
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<UnconfiguredRooms />);

    const first = await screen.findByRole("checkbox", {
      name: "Select Lobby (!first:example.com) for Team",
    });
    const second = screen.getByRole("checkbox", {
      name: "Select Lobby (!second:example.com) for Team",
    });
    await user.click(second);
    expect(first).not.toBeChecked();
    expect(second).toBeChecked();

    await user.click(
      screen.getByRole("button", {
        name: "Open Lobby (!second:example.com) in Matrix client",
      }),
    );
    expect(open).toHaveBeenCalledWith(
      "https://matrix.to/#/!second:example.com",
      "_blank",
    );
    expect(second).toBeChecked();
    open.mockRestore();
  });

  it("renders teams in the external room list", async () => {
    (global.fetch as Mock).mockResolvedValueOnce(
      jsonResponse({
        agents: [
          {
            agent_id: "test_team",
            display_name: "Test Team",
            configured_rooms: ["team_room"],
            joined_rooms: ["team_room", "!external_room:localhost"],
            unconfigured_rooms: ["!external_room:localhost"],
            unconfigured_room_details: [
              { room_id: "!external_room:localhost", name: "Partner Room" },
            ],
          },
        ],
      }),
    );

    render(<UnconfiguredRooms />);

    expect(await screen.findByText("Test Team")).toBeInTheDocument();
    expect(screen.getByText("Partner Room")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Manage rooms that agents and teams have joined but are not in the configuration",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/1 external room found across 1 entity/i),
    ).toBeInTheDocument();
  });

  it("alerts about managed rooms the runtime refused", async () => {
    (global.fetch as Mock).mockResolvedValueOnce(
      jsonResponse({
        agents: [],
        rejected_managed_rooms: {
          "#lobby:localhost":
            "!squatted:localhost: created by @squatter:localhost, not @mindroom_router:localhost",
        },
      }),
    );

    render(<UnconfiguredRooms />);

    expect(
      await screen.findByText(/MindRoom refused these managed rooms/),
    ).toBeInTheDocument();
    expect(screen.getByText("#lobby:localhost")).toBeInTheDocument();
    expect(
      screen.getByText(/created by @squatter:localhost/),
    ).toBeInTheDocument();
  });
});
