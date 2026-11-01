import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Skills } from "./Skills";
import { confirmLeavingUnsavedDrafts } from "@/hooks/useUnsavedDraft";
import {
  createSkill,
  getSkill,
  listSkills,
  updateSkill,
} from "@/services/skillsService";

vi.mock("@/services/skillsService", () => ({
  createSkill: vi.fn(),
  deleteSkill: vi.fn(),
  getSkill: vi.fn(),
  listSkills: vi.fn(),
  updateSkill: vi.fn(),
}));

vi.mock("@/contexts/ThemeContext", () => ({
  useTheme: () => ({ resolvedTheme: "light" }),
}));

vi.mock("@monaco-editor/react", () => ({
  default: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (value: string | undefined) => void;
  }) => (
    <textarea
      aria-label="Skill content"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

const SKILLS = [
  {
    name: "alpha",
    description: "First skill",
    origin: "user" as const,
    can_edit: true,
  },
  {
    name: "beta",
    description: "Second skill",
    origin: "user" as const,
    can_edit: true,
  },
];

async function editAlpha() {
  render(<Skills />);
  fireEvent.click(await screen.findByText("alpha"));
  const editor = await screen.findByLabelText("Skill content");
  await waitFor(() =>
    expect(screen.getByLabelText("Skill content")).toHaveValue("alpha content"),
  );
  fireEvent.change(editor, { target: { value: "unsaved edit" } });
}

describe("Skills unsaved edits", () => {
  const originalInnerWidth = window.innerWidth;

  beforeEach(() => {
    vi.mocked(listSkills).mockResolvedValue(SKILLS);
    vi.mocked(getSkill).mockImplementation(async (name) => ({
      ...SKILLS.find((skill) => skill.name === name)!,
      content: `${name} content`,
    }));
    vi.mocked(createSkill).mockResolvedValue(undefined as never);
    vi.mocked(updateSkill).mockResolvedValue(undefined as never);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.innerWidth = originalInnerWidth;
  });

  it("asks before creating a skill, and Cancel creates nothing", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    await editAlpha();

    fireEvent.click(screen.getByTestId("create-button"));
    fireEvent.change(screen.getByPlaceholderText("Skill name..."), {
      target: { value: "gamma" },
    });
    fireEvent.click(screen.getByTestId("form-create-button"));

    expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");
    expect(createSkill).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Skill content")).toHaveValue("unsaved edit");
    // The form stays open with the name once the create is declined.
    await waitFor(() =>
      expect(screen.getByPlaceholderText("Skill name...")).toBeEnabled(),
    );
    expect(screen.getByPlaceholderText("Skill name...")).toHaveValue("gamma");
  });

  it("creates the skill once the user discards the edits", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await editAlpha();

    fireEvent.click(screen.getByTestId("create-button"));
    fireEvent.change(screen.getByPlaceholderText("Skill name..."), {
      target: { value: "gamma" },
    });
    fireEvent.click(screen.getByTestId("form-create-button"));

    await waitFor(() =>
      expect(createSkill).toHaveBeenCalledWith("gamma", "gamma"),
    );
  });

  it("asks before the back button drops the edits", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    await editAlpha();

    fireEvent.click(screen.getByRole("button", { name: "Back to skills" }));

    expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");
    expect(screen.getByLabelText("Skill content")).toHaveValue("unsaved edit");

    confirmSpy.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Back to skills" }));

    await waitFor(() =>
      expect(screen.queryByLabelText("Skill content")).not.toBeInTheDocument(),
    );
  });

  it("asks before a swipe back drops the edits", async () => {
    window.innerWidth = 400;
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    await editAlpha();

    fireEvent.touchStart(document, {
      touches: [{ clientX: 10, clientY: 100 }],
    });
    fireEvent.touchEnd(document, {
      changedTouches: [{ clientX: 200, clientY: 100 }],
    });

    expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");
    expect(screen.getByLabelText("Skill content")).toHaveValue("unsaved edit");
  });

  it("asks before selecting another skill drops the edits", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    await editAlpha();

    fireEvent.click(screen.getByText("beta"));

    expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");
    expect(screen.getByLabelText("Skill content")).toHaveValue("unsaved edit");
    expect(getSkill).not.toHaveBeenCalledWith("beta");
  });

  it("drops a discarded draft at once, even when the next skill fails to load", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    let failBeta: (error: Error) => void = () => {};
    vi.mocked(getSkill).mockImplementation((name) =>
      name === "beta"
        ? new Promise((_resolve, reject) => {
            failBeta = reject;
          })
        : Promise.resolve({ ...SKILLS[0], content: "alpha content" }),
    );
    await editAlpha();

    fireEvent.click(screen.getByText("beta"));
    expect(confirmSpy).toHaveBeenCalledTimes(1);

    // While beta loads, the discarded draft no longer counts as unsaved.
    expect(confirmLeavingUnsavedDrafts()).toBe(true);
    expect(confirmSpy).toHaveBeenCalledTimes(1);

    failBeta(new Error("unreadable"));
    const editor = await screen.findByLabelText("Skill content");
    expect(editor).toHaveValue("");
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
  });

  it("does not ask when the selected skill is selected again", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    await editAlpha();

    fireEvent.click(screen.getAllByText("alpha")[0]);

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Skill content")).toHaveValue("unsaved edit");
  });

  it("does not ask when nothing is unsaved", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    render(<Skills />);
    fireEvent.click(await screen.findByText("alpha"));
    await waitFor(() =>
      expect(screen.getByLabelText("Skill content")).toHaveValue(
        "alpha content",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Back to skills" }));

    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByLabelText("Skill content")).not.toBeInTheDocument(),
    );
  });
});
