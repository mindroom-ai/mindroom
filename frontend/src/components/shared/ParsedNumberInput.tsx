import { useState, type ComponentProps } from "react";
import { Input } from "@/components/ui/input";

export type ParsedNumberInputProps = Omit<
  ComponentProps<typeof Input>,
  "type" | "value" | "onChange"
> & {
  value: number;
  /** The value typed text stores, such as a clamped number or a fallback. */
  parse: (text: string) => number;
  onChange: (value: number) => void;
};

/**
 * A number input that stores the parsed value on every keystroke but keeps the
 * typed text while it still parses to the stored value, so clearing the field
 * or typing a number digit by digit is not overwritten mid-edit. Leaving the
 * field shows the stored value.
 */
export function ParsedNumberInput({
  value,
  parse,
  onChange,
  onBlur,
  ...props
}: ParsedNumberInputProps) {
  const stored = String(value);
  const [draft, setDraft] = useState(stored);
  // When the stored value changes, keep the draft only if it is what produced
  // that value; a change from elsewhere replaces it.
  const [draftFor, setDraftFor] = useState(stored);
  if (stored !== draftFor) {
    setDraftFor(stored);
    if (parse(draft) !== value) {
      setDraft(stored);
    }
  }
  return (
    <Input
      {...props}
      type="number"
      value={draft}
      onChange={(event) => {
        setDraft(event.target.value);
        onChange(parse(event.target.value));
      }}
      onBlur={(event) => {
        setDraft(stored);
        onBlur?.(event);
      }}
    />
  );
}
