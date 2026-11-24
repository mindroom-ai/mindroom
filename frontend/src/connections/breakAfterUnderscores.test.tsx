import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { breakAfterUnderscores } from "./breakAfterUnderscores";

afterEach(cleanup);

const html = (code: string) => {
  const { container } = render(<span>{breakAfterUnderscores(code)}</span>);
  return container.innerHTML;
};

describe("breaking a refusal code after underscores", () => {
  it("offers a break after each underscore and nowhere inside a word", () => {
    expect(html("credential_not_configured")).toBe(
      "<span>credential_<wbr>not_<wbr>configured</span>",
    );
  });

  it("leaves a code without underscores whole", () => {
    expect(html("refused")).toBe("<span>refused</span>");
  });

  it("keeps the code's text exactly, even with doubled or trailing underscores", () => {
    for (const code of ["a__b", "a_", "_a", ""]) {
      const { container } = render(<span>{breakAfterUnderscores(code)}</span>);
      expect(container.textContent).toBe(code);
      cleanup();
    }
  });
});
