import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ToolInfo } from "@/hooks/useTools";
import { useConfigStore } from "@/store/configStore";

import { DefaultToolSettings } from "./DefaultToolSettings";

vi.mock("@/store/configStore", () => ({ useConfigStore: vi.fn() }));

const TOOLS = [
  {
    name: "gmail",
    display_name: "Gmail",
    config_fields: [{ name: "label", label: "Label", type: "text" }],
  },
  { name: "file", display_name: "File", config_fields: null },
] as unknown as ToolInfo[];

describe("DefaultToolSettings", () => {
  const updateDefaultToolOverrides = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useConfigStore).mockReturnValue({
      config: { defaults: { markdown: true, tools: ["gmail", "file"] } },
      getAgentToolOverrides: vi.fn(),
      updateAgentToolOverrides: vi.fn(),
      getDefaultToolOverrides: vi.fn(() => ({ label: "support" })),
      updateDefaultToolOverrides,
    } as never);
  });

  it("edits overrides for the selected default tool", () => {
    render(<DefaultToolSettings tools={TOOLS} />);
    expect(screen.getByText("Gmail — Default Settings")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: "Override Label" }));
    expect(updateDefaultToolOverrides).toHaveBeenLastCalledWith("gmail", {
      label: null,
    });

    fireEvent.change(screen.getByRole("combobox", { name: "Default tool" }), {
      target: { value: "file" },
    });
    expect(screen.getByText("File — Default Settings")).toBeInTheDocument();
  });

  it("lists default tools authored with overrides by name", () => {
    vi.mocked(useConfigStore).mockReturnValue({
      config: {
        defaults: {
          markdown: true,
          tools: ["file", { gmail: { label: "support" } }],
        },
      },
      getDefaultToolOverrides: vi.fn(() => null),
      updateDefaultToolOverrides,
    } as never);
    render(<DefaultToolSettings tools={TOOLS} />);

    expect(screen.getByRole("option", { name: "Gmail" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Default tool" }), {
      target: { value: "gmail" },
    });
    expect(screen.getByText("Gmail — Default Settings")).toBeInTheDocument();
  });

  it("renders nothing without authored default tools", () => {
    vi.mocked(useConfigStore).mockReturnValue({
      config: { defaults: { markdown: true } },
    } as never);
    const { container } = render(<DefaultToolSettings tools={TOOLS} />);
    expect(container).toBeEmptyDOMElement();
  });
});
