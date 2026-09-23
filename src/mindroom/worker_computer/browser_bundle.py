"""Trusted Computer browser bundle shared by action and native MCP providers.

These paths match the image's pinned browser/server installation. Only the
existing action provider supports an operator executable override; native MCP
always uses this fixed server/browser pairing.
"""

_BUNDLE_ROOT = "/opt/mindroom-browser-mcp"
COMPUTER_BROWSER_EXECUTABLE = f"{_BUNDLE_ROOT}/chromium"
COMPUTER_BROWSER_MCP_SERVER = f"{_BUNDLE_ROOT}/node_modules/@playwright/mcp/cli.js"
COMPUTER_BROWSER_GUARD = f"{_BUNDLE_ROOT}/browser_guard.cjs"
