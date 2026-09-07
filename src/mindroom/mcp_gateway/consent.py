"""Pure rendering for personal gateway consent."""

from __future__ import annotations

from html import escape
from urllib.parse import urlsplit


def render_consent_page(
    *,
    client_name: str,
    agent_name: str,
    redirect_uri: str,
    state: str,
    csrf_token: str,
) -> str:
    """Render escaped consent HTML while displaying only the callback origin."""
    callback = urlsplit(redirect_uri)
    callback_origin = f"{callback.scheme}://{callback.netloc}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect to MindRoom</title>
<style>body{{font:16px system-ui;background:#f7f8fa;color:#17212b;margin:0;padding:8vh 24px}}main{{max-width:560px;margin:auto;padding:32px;background:white;border-radius:16px;box-shadow:0 4px 24px #0001}}button{{font:inherit;padding:10px 20px;margin:12px 12px 0 0;cursor:pointer}}p{{line-height:1.6}}small{{overflow-wrap:anywhere}}</style></head>
<body><main><h1>Connect to MindRoom</h1><p><strong>{escape(client_name)}</strong> wants to use tools from your <strong>{escape(agent_name)}</strong>.</p>
<p>This client can discover and run your assigned tools using your personal connections. Some tools can create or change data. You can connect services separately in Connections.</p>
<p><small>Client callback: {escape(callback_origin)}</small></p><form method="post" action="/connections/mcp/authorize">
<input type="hidden" name="state" value="{escape(state, quote=True)}"><input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">
<button name="decision" value="allow" type="submit">Allow access</button><button name="decision" value="deny" type="submit">Cancel</button></form></main></body></html>"""
