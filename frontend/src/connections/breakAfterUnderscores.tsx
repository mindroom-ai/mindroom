import { Fragment } from "react";

/**
 * A refusal code that can wrap after an underscore and nowhere inside a word,
 * so a long code shows in full, never clipped and never split mid-word.
 */
export function breakAfterUnderscores(code: string) {
  const parts = code.match(/[^_]*_|[^_]+/g) ?? [];
  return parts.map((part, index) => (
    <Fragment key={index}>
      {part}
      {index < parts.length - 1 && <wbr />}
    </Fragment>
  ));
}
