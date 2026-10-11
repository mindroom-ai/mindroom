import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AvatarImage } from "./AvatarImage";
import { EntityAvatar } from "./EntityAvatar";

describe("AvatarImage", () => {
  it("falls back on failed images and retries when source changes", () => {
    const { container, rerender } = render(
      <AvatarImage src="/one" fallback="AB" />,
    );
    expect(container.querySelector("img")).toHaveAttribute("loading", "lazy");
    expect(container.querySelector("img")).toHaveAttribute("alt", "");
    fireEvent.error(container.querySelector("img")!);
    expect(screen.getByText("AB")).toBeVisible();
    expect(container.querySelector("img")).toBeNull();
    rerender(<AvatarImage src="/two" fallback="CD" />);
    expect(container.querySelector("img")).toHaveAttribute("src", "/two");
    rerender(<AvatarImage src="/one" fallback="AB" />);
    expect(container.querySelector("img")).toHaveAttribute("src", "/one");
  });
  it("uses encoded same-origin dashboard paths for entities and configured room references", () => {
    const { container, rerender } = render(
      <EntityAvatar kind="agent" id="helper/bot" name="Helper Bot" />,
    );
    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      "/api/matrix/agents/helper%2Fbot/avatar",
    );
    rerender(<EntityAvatar kind="room" id="#room:server" name="Room" />);
    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      "/api/matrix/rooms/avatar?room_id=%23room%3Aserver",
    );
  });
});
