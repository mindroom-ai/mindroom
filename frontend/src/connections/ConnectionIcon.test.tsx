import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { getIconForTool } from "@/components/Integrations/iconMapping";
import { ConnectionIcon } from "./ConnectionIcon";

afterEach(cleanup);

describe("configured icons", () => {
  it("uses an explicit Lucide icon outside the generated registry", async () => {
    const { container } = render(
      <ConnectionIcon names={["Google Calendar"]} iconName="Book" />,
    );
    await waitFor(() =>
      expect(container.querySelector(".lucide-book")).toBeInTheDocument(),
    );
  });

  it.each(["__proto__", "constructor", "toString", "icons"])(
    "safely falls back for the non-icon name %s on both pages",
    async (iconName) => {
      let connection!: ReturnType<typeof render>;
      let dashboard!: ReturnType<typeof render>;
      await act(async () => {
        connection = render(<ConnectionIcon names={[]} iconName={iconName} />);
        dashboard = render(<>{getIconForTool(iconName)}</>);
      });
      expect(
        connection.container.querySelector(".lucide-plug"),
      ).toBeInTheDocument();
      expect(
        dashboard.container.querySelector(".lucide-globe"),
      ).toBeInTheDocument();
    },
  );
});
