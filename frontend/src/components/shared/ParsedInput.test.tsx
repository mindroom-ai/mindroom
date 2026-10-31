import { useState } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ParsedInput } from "./ParsedInput";

const atLeast128 = (text: string) => {
  const parsed = Number.parseInt(text, 10);
  return Number.isNaN(parsed) || parsed < 128 ? 128 : parsed;
};

function renderInput(start: number) {
  let setFromElsewhere!: (value: number) => void;
  function Harness() {
    const [value, setValue] = useState(start);
    setFromElsewhere = setValue;
    return (
      <ParsedInput
        type="number"
        aria-label="Chunk size"
        value={value}
        format={String}
        parse={atLeast128}
        onChange={setValue}
      />
    );
  }
  render(<Harness />);
  return {
    input: screen.getByLabelText("Chunk size") as HTMLInputElement,
    setFromElsewhere: (value: number) => act(() => setFromElsewhere(value)),
  };
}

describe("ParsedInput", () => {
  it("keeps typed text that parses to the stored value", () => {
    const { input } = renderInput(5000);
    for (const value of ["", "2", "20", "2048"]) {
      fireEvent.change(input, { target: { value } });
      expect(input).toHaveDisplayValue(value);
    }
  });

  it("shows a value stored from elsewhere and the stored value on blur", () => {
    const { input, setFromElsewhere } = renderInput(5000);
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.blur(input);
    expect(input).toHaveDisplayValue("128");

    fireEvent.change(input, { target: { value: "300" } });
    setFromElsewhere(700);
    expect(input).toHaveDisplayValue("700");
  });
});
