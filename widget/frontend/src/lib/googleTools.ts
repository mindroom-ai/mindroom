/**
 * Helper utilities for Google tools management.
 */

/**
 * List of tools that are managed through Google Services OAuth.
 * These tools use the unified Google authentication flow.
 */
const GOOGLE_MANAGED_TOOLS = ['google_calendar', 'google_sheets', 'gmail'];

/**
 * Check if a tool is managed through Google Services OAuth.
 */
export function isGoogleManagedTool(toolName: string): boolean {
  return GOOGLE_MANAGED_TOOLS.includes(toolName);
}

/**
 * Get the list of all Google-managed tools.
 */
export function getGoogleManagedTools(): string[] {
  return [...GOOGLE_MANAGED_TOOLS];
}
