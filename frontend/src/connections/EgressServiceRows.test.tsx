import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EgressServiceRows } from "./EgressServiceRows";
import type { EgressCredentialService } from "./types";

const github: EgressCredentialService = {
  name: "github",
  display_name: "GitHub",
  description: "GitHub API and git over HTTPS",
  is_shared: false,
  can_manage: true,
  configured: false,
  updated_at: null,
};
const openai: EgressCredentialService = {
  ...github,
  name: "openai",
  display_name: "OpenAI",
  description: "OpenAI API",
  configured: true,
  updated_at: null,
};

const noContent = () => new Response(null, { status: 204 });
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const row = (name: string) => screen.getByRole("listitem", { name });

beforeEach(() => {
  vi.mocked(fetch).mockImplementation(async () => noContent());
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("egress service rows", () => {
  it("shows name, description and the Set or Not set status", () => {
    render(
      <EgressServiceRows
        agentName="personal"
        services={[github, openai]}
        onChanged={vi.fn()}
      />,
    );
    expect(within(row("GitHub")).getByText("Not set")).toBeInTheDocument();
    expect(
      within(row("GitHub")).getByText("GitHub API and git over HTTPS"),
    ).toBeInTheDocument();
    expect(within(row("OpenAI")).getByText("Set")).toBeInTheDocument();
    expect(
      within(row("GitHub")).getByRole("button", { name: "Set GitHub API key" }),
    ).toBeInTheDocument();
    expect(
      within(row("OpenAI")).getByRole("button", {
        name: "Replace OpenAI API key",
      }),
    ).toBeInTheDocument();
  });

  it("shows the update date for a key with a known timestamp", () => {
    const updatedAt = "2026-10-05T12:00:00+00:00";
    render(
      <EgressServiceRows
        agentName="personal"
        services={[{ ...openai, updated_at: updatedAt }]}
        onChanged={vi.fn()}
      />,
    );
    expect(
      screen.getByText(`Updated ${new Date(updatedAt).toLocaleDateString()}`),
    ).toBeInTheDocument();
  });

  it("saves the key, clears the input and reports the change", async () => {
    const onChanged = vi.fn();
    render(
      <EgressServiceRows
        agentName="personal"
        services={[github]}
        onChanged={onChanged}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    const input = screen.getByLabelText("GitHub API key");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.change(input, { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith(
      "/api/connections/egress/agents/personal/github",
      expect.objectContaining({
        method: "PUT",
        credentials: "same-origin",
        body: JSON.stringify({ secret: "abc" }),
      }),
    );
    expect(screen.queryByLabelText("GitHub API key")).toBeNull();
    // Reopening the editor must not show the previous value.
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    expect(screen.getByLabelText("GitHub API key")).toHaveValue("");
  });

  it("encodes the agent and service names in the request path", async () => {
    render(
      <EgressServiceRows
        agentName="my agent"
        services={[{ ...github, name: "git/hub" }]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(
      "/api/connections/egress/agents/my%20agent/git%2Fhub",
    );
  });

  it("writes to a custom secret path instead of the connections API", async () => {
    render(
      <EgressServiceRows
        secretPath={(name) => `/custom/${name}/secret`}
        services={[github]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe("/custom/github/secret");
  });

  it("cancels without sending anything and drops the typed value", () => {
    render(
      <EgressServiceRows
        agentName="personal"
        services={[openai]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Replace OpenAI API key" }),
    );
    fireEvent.change(screen.getByLabelText("OpenAI API key"), {
      target: { value: "sk-typed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("OpenAI API key")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Replace OpenAI API key" }),
    );
    expect(screen.getByLabelText("OpenAI API key")).toHaveValue("");
  });

  it("keeps Save disabled until a key is typed", () => {
    render(
      <EgressServiceRows
        agentName="personal"
        services={[github]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "   " },
    });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("shows the server validation message and keeps the editor open on failure", async () => {
    const onChanged = vi.fn();
    vi.mocked(fetch).mockImplementation(async () =>
      json({ detail: "Secret cannot be empty or whitespace" }, 422),
    );
    render(
      <EgressServiceRows
        agentName="personal"
        services={[github]}
        onChanged={onChanged}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Secret cannot be empty or whitespace",
    );
    expect(onChanged).not.toHaveBeenCalled();
    expect(screen.getByLabelText("GitHub API key")).toBeInTheDocument();
  });

  it("removes a key only after confirmation", async () => {
    const onChanged = vi.fn();
    render(
      <EgressServiceRows
        agentName="personal"
        services={[openai]}
        onChanged={onChanged}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Remove OpenAI API key" }),
    );
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith(
      "/api/connections/egress/agents/personal/openai",
      expect.objectContaining({
        method: "DELETE",
        credentials: "same-origin",
      }),
    );
  });

  it("does not remove the key when the confirmation is cancelled", async () => {
    render(
      <EgressServiceRows
        agentName="personal"
        services={[openai]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Remove OpenAI API key" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("offers no Remove for a service without a key", () => {
    render(
      <EgressServiceRows
        agentName="personal"
        services={[github]}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /Remove/ })).toBeNull();
  });

  it("shows status only to users who cannot manage the shared key", () => {
    render(
      <EgressServiceRows
        agentName="shared_dev"
        services={[{ ...openai, is_shared: true, can_manage: false }]}
        onChanged={vi.fn()}
      />,
    );
    expect(within(row("OpenAI")).getByText("Set")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Managed by credential managers")).toBeVisible();
  });

  it("warns that a shared key affects everyone who relies on it", async () => {
    render(
      <EgressServiceRows
        agentName="shared_dev"
        services={[{ ...openai, is_shared: true, can_manage: true }]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Remove OpenAI API key" }),
    );
    expect(
      await screen.findByText(
        "This deletes the shared key. Everyone who relies on it loses access to this service until a key is set again.",
      ),
    ).toBeInTheDocument();
  });

  it("says the key is not shared when the scope is unknown", async () => {
    render(
      <EgressServiceRows
        agentName="personal"
        services={[{ ...openai, is_shared: null }]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Remove OpenAI API key" }),
    );
    expect(
      await screen.findByText(
        "This deletes the saved key. The agent loses access to this service until a key is set again.",
      ),
    ).toBeInTheDocument();
  });

  it("reports a forbidden save with the connections wording by default", async () => {
    vi.mocked(fetch).mockImplementation(async () => json({}, 403));
    render(
      <EgressServiceRows
        agentName="personal"
        services={[github]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(
        "Connections are not available for this account.",
      ),
    ).toBeInTheDocument();
  });

  it("uses the supplied wording for 403 and 404 responses", async () => {
    render(
      <EgressServiceRows
        secretPath={(name) => `/custom/${name}/secret`}
        errorMessages={{ forbidden: "No access.", notFound: "Gone." }}
        services={[openai]}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Remove OpenAI API key" }),
    );
    vi.mocked(fetch).mockImplementation(async () => json({}, 404));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Remove",
      }),
    );
    expect(await screen.findByText("Gone.")).toBeInTheDocument();

    vi.mocked(fetch).mockImplementation(async () => json({}, 403));
    fireEvent.click(
      screen.getByRole("button", { name: "Remove OpenAI API key" }),
    );
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Remove",
      }),
    );
    expect(await screen.findByText("No access.")).toBeInTheDocument();
  });
});
