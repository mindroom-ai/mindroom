import { useState, type ComponentProps, type FocusEvent } from "react";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

interface ParsedValueProps<T> {
  value: T;
  /** The text the field shows for a stored value. */
  format: (value: T) => string;
  /** The value typed text stores, such as a trimmed, clamped, or default value. */
  parse: (text: string) => T;
  onChange: (value: T) => void;
}

/**
 * Text for a field whose value the editor stores normalized. The field stores
 * the parsed value on every keystroke but keeps the typed text, such as a
 * trailing space or a number typed digit by digit, while the stored value is
 * the one it sent. A value stored from elsewhere replaces the text, and
 * leaving the field shows the stored value.
 */
function useParsedText<T>({
  value,
  format,
  parse,
  onChange,
}: ParsedValueProps<T>) {
  const stored = format(value);
  const [draft, setDraft] = useState(stored);
  // The stored text the draft stands for.
  const [draftFor, setDraftFor] = useState(stored);
  if (stored !== draftFor) {
    setDraftFor(stored);
    setDraft(stored);
  }
  return {
    value: draft,
    onChange: (event: { target: { value: string } }) => {
      const next = parse(event.target.value);
      setDraft(event.target.value);
      setDraftFor(format(next));
      onChange(next);
    },
    onBlur: () => setDraft(stored),
  };
}

type FieldProps<Field extends typeof Input | typeof Textarea, T> = Omit<
  ComponentProps<Field>,
  "value" | "onChange"
> &
  ParsedValueProps<T>;

export function ParsedInput<T>({
  value,
  format,
  parse,
  onChange,
  onBlur,
  ...props
}: FieldProps<typeof Input, T>) {
  const text = useParsedText({ value, format, parse, onChange });
  return (
    <Input
      {...props}
      {...text}
      onBlur={(event: FocusEvent<HTMLInputElement>) => {
        text.onBlur();
        onBlur?.(event);
      }}
    />
  );
}

export function ParsedTextarea<T>({
  value,
  format,
  parse,
  onChange,
  onBlur,
  ...props
}: FieldProps<typeof Textarea, T>) {
  const text = useParsedText({ value, format, parse, onChange });
  return (
    <Textarea
      {...props}
      {...text}
      onBlur={(event: FocusEvent<HTMLTextAreaElement>) => {
        text.onBlur();
        onBlur?.(event);
      }}
    />
  );
}
