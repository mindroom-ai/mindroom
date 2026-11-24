import type { EgressPlaceholderValidation } from "./types";

/**
 * What a placeholder name and value must look like, and the texts that say
 * why not. The server decides: its words, reserved names, and name and value
 * patterns arrive in `validation.placeholder`, so these checks give the same
 * answer, with the same words, while the form is being filled in.
 */

// Every service's variable names, as the config model checks them.
export const PLACEHOLDER_NAME_PATTERN = /^[A-Z_][A-Z0-9_]*$/;

/** What the dashboard says about a name outside `PLACEHOLDER_NAME_PATTERN`, quoting the pattern as the server does. */
export function operatorPlaceholderNameError(name: string): string {
  return `Placeholder name ${name} must match ${PLACEHOLDER_NAME_PATTERN.source}`;
}

/** Why no service may use this valid placeholder name, or null; the server refuses these in every scope. */
export function reservedPlaceholderError(
  name: string,
  validation: EgressPlaceholderValidation,
): string | null {
  if (validation.reserved_names.includes(name))
    return `Placeholder name ${name} is reserved`;
  const prefix = validation.reserved_prefixes.find((reserved) =>
    name.startsWith(reserved),
  );
  return prefix
    ? `Placeholder name ${name} starts with reserved prefix ${prefix}`
    : null;
}

/** Join words as `A, B, or C`, the way the server's messages do. */
function wordList(words: readonly string[]): string {
  return `${words.slice(0, -1).join(", ")}, or ${words[words.length - 1]}`;
}

/** Join words for a hint: `A`, `A or B`, or `A, B, or C`. */
function orList(words: readonly string[]): string {
  if (words.length < 3) return words.join(" or ");
  return wordList(words);
}

/** What a value may look like, `1 to 256 letters, digits, or . _ : -`, with the length the server serves. */
function valueDescription(validation: EgressPlaceholderValidation): string {
  return `1 to ${validation.value_max_length} letters, digits, or . _ : -`;
}

/** The hint under the placeholder rows of a personal service; a short one without the rules when the presets could not be loaded. */
export function personalPlaceholderHint(
  validation: EgressPlaceholderValidation | null,
): string {
  if (!validation)
    return "Names use capital letters, digits, and underscores, and must name a credential, such as MY_API_KEY. The server checks the rest when you save.";
  const prefixes = orList([
    validation.refused_prefix,
    ...validation.reserved_prefixes,
  ]);
  return `Names use capital letters, digits, and underscores, and one word between underscores must be ${orList(validation.credential_words)} (for example MY_API_KEY). No word may be ${orList(validation.refused_words)}, and names cannot start with ${prefixes}. PATH, HOME, and proxy and CA variables are reserved. Values are ${valueDescription(validation)}`;
}

/** The hint under the placeholder rows of the administrator's service; a short one without the prefixes when the presets could not be loaded. */
export function operatorPlaceholderHint(
  validation: EgressPlaceholderValidation | null,
): string {
  const reserved = validation
    ? `PATH, HOME, proxy and CA variables, and names starting with ${orList(validation.reserved_prefixes)} are reserved.`
    : "Some names, such as PATH and HOME, are reserved.";
  return `Names use capital letters, digits, and underscores. ${reserved}`;
}

export function personalPlaceholderNameError(
  name: string,
  validation: EgressPlaceholderValidation,
): string {
  return `${name} is not allowed as a placeholder: name a credential, such as MY_API_KEY, with ${wordList(validation.credential_words)} as one of its words.`;
}

/** A server pattern, which Python matches against the whole name or value, as a regular expression that does too. */
function wholeMatchPattern(source: string): RegExp | null {
  try {
    return new RegExp(`^(?:${source})$`);
  } catch {
    // A pattern this browser cannot read is left to the server.
    return null;
  }
}

/**
 * Why a personal service may not set this placeholder, as the part of the row
 * that is wrong and the message; null when it may. A personal service may only
 * set credential-like names, with plain values. The texts mirror the server's,
 * which never quotes a value.
 */
export function personalPlaceholderError(
  name: string,
  value: string,
  validation: EgressPlaceholderValidation,
): { part: "name" | "value"; message: string } | null {
  const words = name.split("_");
  const namePattern = wholeMatchPattern(validation.name_pattern);
  if (
    (namePattern !== null && !namePattern.test(name)) ||
    !validation.credential_words.some((word) => words.includes(word))
  )
    return {
      part: "name",
      message: personalPlaceholderNameError(name, validation),
    };
  // Tools read variables with these words, or starting with the prefix, as
  // programs or paths to run or load, so a service of one's own cannot set them.
  if (
    name.startsWith(validation.refused_prefix) ||
    validation.refused_words.some((word) => words.includes(word))
  )
    return {
      part: "name",
      message: `${name} is not allowed: tools read names with ${wordList(validation.refused_words)}, or starting with ${validation.refused_prefix}, as programs or paths.`,
    };
  const valuePattern = wholeMatchPattern(validation.value_pattern);
  if (valuePattern !== null && !valuePattern.test(value))
    return {
      part: "value",
      message: `The value of ${name} must be ${valueDescription(validation)}`,
    };
  return null;
}
