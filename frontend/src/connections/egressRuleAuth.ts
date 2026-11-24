import type { EgressRuleAuthType } from "./types";

/**
 * How a rule sends the key: the ways the form offers, the `{secret}` value
 * template, and what makes a header name or a template acceptable.
 */

/** Where a value template puts the key; it must appear in the template exactly once. */
export const DEFAULT_TEMPLATE = "{secret}";

export const AUTH_CHOICES: readonly {
  value: EgressRuleAuthType;
  label: string;
}[] = [
  { value: "bearer", label: "Bearer token" },
  { value: "header", label: "Header" },
  { value: "basic", label: "Basic" },
  { value: "query", label: "Query parameter" },
  { value: "template", label: "Custom template" },
];

/** Whether a template is one of the user's own, not empty and not the plain `{secret}`. */
export function hasCustomTemplate(template: string | undefined): boolean {
  return (
    template !== undefined && template !== "" && template !== DEFAULT_TEMPLATE
  );
}

const HEADER_NAME_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
// Headers that frame the message, route it, or authenticate to the proxy belong to the broker.
const RESERVED_HEADERS = new Set([
  "host",
  "content-length",
  "transfer-encoding",
  "connection",
  "upgrade",
  "te",
  "trailer",
  "proxy-authorization",
  "proxy-connection",
  "keep-alive",
]);

/** Why this cannot be the name of the header a key goes in, or null when it can. */
export function headerNameError(name: string): string | null {
  if (!name) return "Header auth needs a header name";
  if (!HEADER_NAME_PATTERN.test(name))
    return "Header name must be a valid HTTP header name";
  if (RESERVED_HEADERS.has(name.toLowerCase()))
    return `Header name ${name} is reserved for the broker`;
  return null;
}

/** Why a value template cannot be used, or null; an empty one is fine unless `required`. */
export function templateError(
  template: string,
  required: boolean,
): string | null {
  if (template === "" && !required) return null;
  return template.split(DEFAULT_TEMPLATE).length - 1 === 1
    ? null
    : "Template must contain {secret} exactly once";
}
