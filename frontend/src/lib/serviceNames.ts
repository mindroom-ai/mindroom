// The characters a credential service name may contain, as the backend's credential store allows them.
// A model name becomes part of its dashboard key's service name, `model:<name>`, so it follows the same rule.
export const SERVICE_NAME_PATTERN = /^[a-zA-Z0-9:_-]+$/;
export const SERVICE_NAME_CHARACTERS =
  "letters, numbers, colon, underscore, and hyphen";
