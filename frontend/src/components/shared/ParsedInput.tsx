import { useState, type ComponentProps } from "react";
import { Input } from "@/components/ui/input";

export type ParsedInputProps<T> = Omit<
  ComponentProps<typeof Input>,
  "value" | "onChange"
> & {
  value: T;
  /** The text the input shows for a stored value. */
  format: (value: T) => string;
  /** The value typed text stores, such as a trimmed, clamped, or default value. */
  parse: (text: string) => T;
  onChange: (value: T) => void;
};

/**
 * An input for a value the editor stores normalized. It stores the parsed
 * value on every keystroke but keeps the typed text, such as a trailing space
 * or a number typed digit by digit, while the stored value is the one this
 * input sent. A value stored from elsewhere replaces the text, and leaving the
 * field shows the stored value.
 */
export function ParsedInput<T>({
  value,
  format,
  parse,
  onChange,
  onBlur,
  ...props
}: ParsedInputProps<T>) {
  const stored = format(value);
  const [draft, setDraft] = useState(stored);
  // The stored text the draft stands for.
  const [draftFor, setDraftFor] = useState(stored);
  if (stored !== draftFor) {
    setDraftFor(stored);
    setDraft(stored);
  }
  return (
    <Input
      {...props}
      value={draft}
      onChange={(event) => {
        const next = parse(event.target.value);
        setDraft(event.target.value);
        setDraftFor(format(next));
        onChange(next);
      }}
      onBlur={(event) => {
        setDraft(stored);
        onBlur?.(event);
      }}
    />
  );
}
