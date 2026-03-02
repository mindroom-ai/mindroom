"""OpenClaw-style browser tool for MindRoom.

This exposes a single ``browser`` function with an ``action`` parameter,
matching OpenClaw's tool surface at a high level.
"""
# ruff: noqa: N803, A002

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agno.tools import Toolkit
from playwright.async_api import Browser, BrowserContext, ConsoleMessage, Dialog, Page, Playwright, async_playwright

from mindroom.constants import STORAGE_PATH_OBJ

_DEFAULT_PROFILE = "openclaw"
_DEFAULT_SNAPSHOT_LIMIT = 200
_DEFAULT_AI_SNAPSHOT_MAX_CHARS = 12_000
_DEFAULT_TIMEOUT_MS = 30_000
_MAX_CONSOLE_ENTRIES = 200
_VIEWPORT_WIDTH = 1280
_VIEWPORT_HEIGHT = 720

_BROWSER_ACTIONS = {
    "status",
    "start",
    "stop",
    "profiles",
    "tabs",
    "open",
    "focus",
    "close",
    "snapshot",
    "screenshot",
    "navigate",
    "console",
    "pdf",
    "upload",
    "dialog",
    "act",
}

_SNAPSHOT_JS = """
({ selector, limit, depth, interactiveOnly }) => {
  const root = selector ? document.querySelector(selector) : (document.body || document.documentElement);
  if (!root) return [];

  const maxItems = Math.max(1, Number.isFinite(limit) ? Number(limit) : 200);
  const maxDepth = Math.max(1, Number.isFinite(depth) ? Number(depth) : 12);

  const isVisible = (el) => {
    if (!(el instanceof HTMLElement)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const isInteractive = (el) => {
    const tag = el.tagName.toLowerCase();
    if (["a", "button", "input", "select", "textarea", "summary", "option", "label"].includes(tag)) return true;
    if (el.hasAttribute("contenteditable")) return true;
    if (el.hasAttribute("onclick")) return true;
    const role = (el.getAttribute("role") || "").trim();
    return role.length > 0;
  };

  const inferRole = (el) => {
    const explicit = (el.getAttribute("role") || "").trim();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "input") return "input";
    if (tag === "select") return "select";
    if (tag === "textarea") return "textbox";
    return "";
  };

  const selectorFor = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    let node = el;
    let safety = 0;
    while (node && node.nodeType === Node.ELEMENT_NODE && safety < 12) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((sib) => sib.tagName === node.tagName);
        if (siblings.length > 1) {
          const idx = siblings.indexOf(node) + 1;
          part += `:nth-of-type(${idx})`;
        }
      }
      parts.unshift(part);
      if (node.id) {
        parts[0] = `#${CSS.escape(node.id)}`;
        break;
      }
      node = parent;
      safety += 1;
    }
    return parts.join(" > ");
  };

  const depthFromRoot = (el) => {
    let d = 0;
    let node = el;
    while (node && node !== root) {
      d += 1;
      node = node.parentElement;
      if (d > 200) break;
    }
    return d;
  };

  const candidates = [root, ...root.querySelectorAll("*")];
  const rows = [];
  for (const el of candidates) {
    if (!(el instanceof HTMLElement)) continue;
    if (!isVisible(el)) continue;
    if (depthFromRoot(el) > maxDepth) continue;
    if (interactiveOnly && !isInteractive(el)) continue;
    const text = (
      el.getAttribute("aria-label") ||
      el.getAttribute("alt") ||
      el.getAttribute("title") ||
      el.innerText ||
      el.textContent ||
      el.getAttribute("value") ||
      ""
    ).replace(/\\s+/g, " ").trim();
    rows.push({
      selector: selectorFor(el),
      role: inferRole(el),
      name: text.slice(0, 140),
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute("type") || "").trim(),
    });
    if (rows.length >= maxItems) break;
  }
  return rows;
}
"""


@dataclass
class _BrowserTabState:
    """State for one browser tab."""

    target_id: str
    page: Page
    refs: dict[str, str] = field(default_factory=dict)
    pending_dialog: dict[str, Any] | None = None
    console: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _BrowserProfileState:
    """State for one browser profile."""

    playwright: Playwright
    browser: Browser
    context: BrowserContext
    tabs: dict[str, _BrowserTabState] = field(default_factory=dict)
    active_target_id: str | None = None


def _clean_str(value: object) -> str | None:
    """Normalize a value to a stripped string."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


class BrowserTools(Toolkit):
    """OpenClaw-style browser control for MindRoom agents."""

    def __init__(self) -> None:
        super().__init__(name="browser", tools=[self.browser])
        self._profiles: dict[str, _BrowserProfileState] = {}
        self._lock = asyncio.Lock()
        self._output_dir = STORAGE_PATH_OBJ / "browser"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._close_task: asyncio.Task[None] | None = None

    async def _close_profiles(self) -> None:
        """Close all active browser profiles."""
        for profile_name in list(self._profiles.keys()):
            await self._stop_profile(profile_name)

    def close(self) -> None:
        """Close toolkit resources."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._close_profiles())
            return
        self._close_task = loop.create_task(self._close_profiles())

    async def browser(  # noqa: C901, PLR0911, PLR0912
        self,
        action: str,
        target: str | None = None,
        node: str | None = None,
        profile: str | None = None,
        targetUrl: str | None = None,
        targetId: str | None = None,
        limit: int | None = None,
        maxChars: int | None = None,
        mode: str | None = None,
        snapshotFormat: str | None = None,
        refs: str | None = None,
        interactive: bool | None = None,
        compact: bool | None = None,
        depth: int | None = None,
        selector: str | None = None,
        frame: str | None = None,
        labels: bool | None = None,
        fullPage: bool | None = None,
        ref: str | None = None,
        element: str | None = None,
        type: str | None = None,
        level: str | None = None,
        paths: list[str] | None = None,
        inputRef: str | None = None,
        timeoutMs: int | None = None,
        accept: bool | None = None,
        promptText: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> str:
        """Control browser state and actions.

        Args:
            action: Browser action (status/start/stop/profiles/tabs/open/focus/close/snapshot/screenshot/navigate/console/pdf/upload/dialog/act)
            target: Browser target location. Only ``host`` is currently supported.
            node: Node id (OpenClaw compatibility field; unsupported in MindRoom runtime).
            profile: Browser profile name (defaults to ``openclaw``).
            targetUrl: URL for ``open`` and ``navigate`` actions.
            targetId: Tab target id for actions that address a specific tab.
            limit: Snapshot item limit.
            maxChars: Snapshot text limit.
            mode: Snapshot mode (supports ``efficient``).
            snapshotFormat: ``ai`` or ``aria``.
            refs: Snapshot refs mode (accepted for compatibility).
            interactive: Snapshot interactive filtering.
            compact: Snapshot compact mode hint.
            depth: Snapshot traversal depth.
            selector: Root selector for snapshot.
            frame: Frame selector (accepted for compatibility; ignored for now).
            labels: Snapshot label hint (accepted for compatibility; ignored for now).
            fullPage: Full-page capture for screenshots.
            ref: Snapshot ref id or CSS selector.
            element: CSS selector for element-specific actions.
            type: Screenshot type (``png`` or ``jpeg``).
            level: Console log level filter.
            paths: Upload file paths.
            inputRef: Upload input selector or ref.
            timeoutMs: Timeout for wait/upload/dialog actions.
            accept: Whether to accept dialog.
            promptText: Prompt text for dialog accept.
            request: Act request object.

        Returns:
            JSON-encoded result payload.

        """
        normalized_action = action.strip().lower()
        if normalized_action not in _BROWSER_ACTIONS:
            msg = f"Unknown action: {action}"
            raise ValueError(msg)

        self._validate_target(target=target, node=node)
        profile_name = _clean_str(profile) or _DEFAULT_PROFILE

        if normalized_action == "status":
            return json.dumps(await self._status_payload(profile_name), sort_keys=True)
        if normalized_action == "start":
            state = await self._ensure_profile(profile_name)
            payload = await self._profile_status(profile_name, state)
            payload["action"] = "start"
            return json.dumps(payload, sort_keys=True)
        if normalized_action == "stop":
            await self._stop_profile(profile_name)
            return json.dumps({"action": "stop", "profile": profile_name, "status": "ok"}, sort_keys=True)
        if normalized_action == "profiles":
            return json.dumps(await self._profiles_payload(profile_name), sort_keys=True)
        if normalized_action == "tabs":
            state = await self._ensure_profile(profile_name)
            return json.dumps(await self._tabs_payload(profile_name, state), sort_keys=True)
        if normalized_action == "open":
            target_url = _clean_str(targetUrl)
            if target_url is None:
                msg = "targetUrl required for action=open"
                raise ValueError(msg)
            return json.dumps(await self._open_tab(profile_name, target_url), sort_keys=True)
        if normalized_action == "focus":
            target_id = _clean_str(targetId)
            if target_id is None:
                msg = "targetId required for action=focus"
                raise ValueError(msg)
            return json.dumps(await self._focus_tab(profile_name, target_id), sort_keys=True)
        if normalized_action == "close":
            return json.dumps(await self._close_tab(profile_name, _clean_str(targetId)), sort_keys=True)
        if normalized_action == "snapshot":
            return json.dumps(
                await self._snapshot(
                    profile_name=profile_name,
                    target_id=_clean_str(targetId),
                    snapshot_format=_clean_str(snapshotFormat),
                    limit=limit,
                    max_chars=maxChars,
                    mode=_clean_str(mode),
                    refs_mode=_clean_str(refs),
                    interactive=interactive,
                    compact=compact,
                    depth=depth,
                    selector=_clean_str(selector),
                    frame=_clean_str(frame),
                    labels=labels,
                ),
                sort_keys=True,
            )
        if normalized_action == "screenshot":
            return json.dumps(
                await self._screenshot(
                    profile_name=profile_name,
                    target_id=_clean_str(targetId),
                    full_page=bool(fullPage),
                    ref=_clean_str(ref),
                    element=_clean_str(element),
                    image_type=_clean_str(type),
                ),
                sort_keys=True,
            )
        if normalized_action == "navigate":
            target_url = _clean_str(targetUrl)
            if target_url is None:
                msg = "targetUrl required for action=navigate"
                raise ValueError(msg)
            return json.dumps(
                await self._navigate(profile_name, target_url, _clean_str(targetId)),
                sort_keys=True,
            )
        if normalized_action == "console":
            return json.dumps(
                await self._console(
                    profile_name=profile_name,
                    target_id=_clean_str(targetId),
                    level=_clean_str(level),
                ),
                sort_keys=True,
            )
        if normalized_action == "pdf":
            return json.dumps(await self._pdf(profile_name, _clean_str(targetId)), sort_keys=True)
        if normalized_action == "upload":
            if not paths:
                msg = "paths required for action=upload"
                raise ValueError(msg)
            return json.dumps(
                await self._upload(
                    profile_name=profile_name,
                    target_id=_clean_str(targetId),
                    paths=paths,
                    ref=_clean_str(ref),
                    input_ref=_clean_str(inputRef),
                    element=_clean_str(element),
                    timeout_ms=timeoutMs,
                ),
                sort_keys=True,
            )
        if normalized_action == "dialog":
            return json.dumps(
                await self._dialog(
                    profile_name=profile_name,
                    target_id=_clean_str(targetId),
                    accept=bool(accept),
                    prompt_text=_clean_str(promptText),
                    timeout_ms=timeoutMs,
                ),
                sort_keys=True,
            )
        if normalized_action == "act":
            if not isinstance(request, dict):
                msg = "request required for action=act"
                raise ValueError(msg)
            return json.dumps(
                await self._act(
                    profile_name=profile_name,
                    request=request,
                    fallback_target_id=_clean_str(targetId),
                ),
                sort_keys=True,
            )

        msg = f"Unhandled action: {action}"
        raise ValueError(msg)

    @staticmethod
    def _validate_target(*, target: str | None, node: str | None) -> None:
        normalized_target = _clean_str(target)
        if node is not None and normalized_target not in {None, "node"}:
            msg = "node parameter is not supported in MindRoom."
            raise ValueError(msg)
        if normalized_target in {"sandbox", "node"} or node is not None:
            msg = "MindRoom browser tool currently supports host target only."
            raise ValueError(msg)
        if normalized_target not in {None, "host"}:
            msg = f"Unsupported target: {target}"
            raise ValueError(msg)

    async def _status_payload(self, profile_name: str) -> dict[str, Any]:
        async with self._lock:
            state = self._profiles.get(profile_name)
            if state is None:
                return {"action": "status", "profile": profile_name, "running": False, "status": "ok", "tabs": []}
            return await self._profile_status(profile_name, state)

    async def _profiles_payload(self, selected_profile: str) -> dict[str, Any]:
        async with self._lock:
            running = sorted(self._profiles.keys())
        advertised = sorted({_DEFAULT_PROFILE, "chrome", *running})
        return {
            "action": "profiles",
            "profiles": advertised,
            "running_profiles": running,
            "selected_profile": selected_profile,
            "status": "ok",
        }

    async def _profile_status(self, profile_name: str, state: _BrowserProfileState) -> dict[str, Any]:
        tabs = await self._tab_list(state)
        return {
            "action": "status",
            "activeTargetId": state.active_target_id,
            "profile": profile_name,
            "running": True,
            "status": "ok",
            "tabCount": len(tabs),
            "tabs": tabs,
        }

    async def _tab_list(self, state: _BrowserProfileState) -> list[dict[str, Any]]:
        payload_tabs: list[dict[str, Any]] = []
        stale: list[str] = []
        for target_id, tab in state.tabs.items():
            if tab.page.is_closed():
                stale.append(target_id)
                continue
            title = await tab.page.title()
            payload_tabs.append(
                {
                    "active": target_id == state.active_target_id,
                    "targetId": target_id,
                    "title": title,
                    "url": tab.page.url,
                },
            )
        for target_id in stale:
            self._remove_tab(state, target_id)
        return payload_tabs

    async def _tabs_payload(self, profile_name: str, state: _BrowserProfileState) -> dict[str, Any]:
        tabs = await self._tab_list(state)
        return {
            "action": "tabs",
            "activeTargetId": state.active_target_id,
            "profile": profile_name,
            "status": "ok",
            "tabs": tabs,
        }

    async def _open_tab(self, profile_name: str, target_url: str) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        page = await state.context.new_page()
        target_id = self._register_tab(state, page)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=_DEFAULT_TIMEOUT_MS)
        state.active_target_id = target_id
        return {
            "action": "open",
            "profile": profile_name,
            "status": "ok",
            "targetId": target_id,
            "title": await page.title(),
            "url": page.url,
        }

    async def _focus_tab(self, profile_name: str, target_id: str) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        if target_id not in state.tabs or state.tabs[target_id].page.is_closed():
            msg = f"tab not found: {target_id}"
            raise ValueError(msg)
        state.active_target_id = target_id
        page = state.tabs[target_id].page
        return {
            "action": "focus",
            "profile": profile_name,
            "status": "ok",
            "targetId": target_id,
            "title": await page.title(),
            "url": page.url,
        }

    async def _close_tab(self, profile_name: str, target_id: str | None) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        resolved_target_id, tab = await self._resolve_tab(state, target_id)
        await tab.page.close()
        self._remove_tab(state, resolved_target_id)
        return {
            "action": "close",
            "profile": profile_name,
            "status": "ok",
            "targetId": resolved_target_id,
        }

    async def _navigate(self, profile_name: str, target_url: str, target_id: str | None) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        resolved_target_id, tab = await self._resolve_tab(state, target_id)
        await tab.page.goto(target_url, wait_until="domcontentloaded", timeout=_DEFAULT_TIMEOUT_MS)
        state.active_target_id = resolved_target_id
        return {
            "action": "navigate",
            "profile": profile_name,
            "status": "ok",
            "targetId": resolved_target_id,
            "title": await tab.page.title(),
            "url": tab.page.url,
        }

    async def _console(self, profile_name: str, target_id: str | None, level: str | None) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        if target_id is not None:
            _, tab = await self._resolve_tab(state, target_id)
            entries = tab.console
        else:
            entries = [entry for tab in state.tabs.values() for entry in tab.console]
        if level is not None:
            entries = [entry for entry in entries if entry.get("level") == level]
        return {
            "action": "console",
            "entries": entries[-_MAX_CONSOLE_ENTRIES:],
            "level": level,
            "profile": profile_name,
            "status": "ok",
            "targetId": target_id,
        }

    async def _pdf(self, profile_name: str, target_id: str | None) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        resolved_target_id, tab = await self._resolve_tab(state, target_id)
        output_path = self._next_output_path("pdf")
        await tab.page.pdf(path=str(output_path))
        return {
            "action": "pdf",
            "path": str(output_path),
            "profile": profile_name,
            "status": "ok",
            "targetId": resolved_target_id,
        }

    async def _upload(
        self,
        *,
        profile_name: str,
        target_id: str | None,
        paths: list[str],
        ref: str | None,
        input_ref: str | None,
        element: str | None,
        timeout_ms: int | None,
    ) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        resolved_target_id, tab = await self._resolve_tab(state, target_id)
        selector = self._resolve_selector(tab, input_ref or ref or element)
        if selector is None:
            msg = "upload requires inputRef, ref, or element"
            raise ValueError(msg)
        normalized_paths = [str(Path(path).expanduser()) for path in paths]
        locator = tab.page.locator(selector).first
        await locator.set_input_files(normalized_paths, timeout=timeout_ms or _DEFAULT_TIMEOUT_MS)
        return {
            "action": "upload",
            "paths": normalized_paths,
            "profile": profile_name,
            "selector": selector,
            "status": "ok",
            "targetId": resolved_target_id,
        }

    async def _dialog(
        self,
        *,
        profile_name: str,
        target_id: str | None,
        accept: bool,
        prompt_text: str | None,
        timeout_ms: int | None,
    ) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        resolved_target_id, tab = await self._resolve_tab(state, target_id)
        tab.pending_dialog = {
            "accept": accept,
            "promptText": prompt_text,
            "timeoutMs": timeout_ms,
        }
        return {
            "accept": accept,
            "action": "dialog",
            "armed": True,
            "profile": profile_name,
            "promptText": prompt_text,
            "status": "ok",
            "targetId": resolved_target_id,
            "timeoutMs": timeout_ms,
        }

    async def _screenshot(
        self,
        *,
        profile_name: str,
        target_id: str | None,
        full_page: bool,
        ref: str | None,
        element: str | None,
        image_type: str | None,
    ) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        resolved_target_id, tab = await self._resolve_tab(state, target_id)
        resolved_type = "jpeg" if image_type == "jpeg" else "png"
        output_path = self._next_output_path("jpg" if resolved_type == "jpeg" else "png")
        selector = self._resolve_selector(tab, element or ref)
        if selector is None:
            await tab.page.screenshot(path=str(output_path), type=resolved_type, full_page=full_page)
        else:
            await tab.page.locator(selector).first.screenshot(path=str(output_path), type=resolved_type)
        return {
            "action": "screenshot",
            "fullPage": full_page,
            "path": str(output_path),
            "profile": profile_name,
            "selector": selector,
            "status": "ok",
            "targetId": resolved_target_id,
            "type": resolved_type,
        }

    async def _snapshot(
        self,
        *,
        profile_name: str,
        target_id: str | None,
        snapshot_format: str | None,
        limit: int | None,
        max_chars: int | None,
        mode: str | None,
        refs_mode: str | None,
        interactive: bool | None,
        compact: bool | None,
        depth: int | None,
        selector: str | None,
        frame: str | None,
        labels: bool | None,
    ) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        resolved_target_id, tab = await self._resolve_tab(state, target_id)
        if frame is not None:
            del frame  # Not implemented in local Playwright path yet.
        if labels is not None:
            del labels  # Label overlays are currently not implemented.

        fmt = "aria" if snapshot_format == "aria" else "ai"
        resolved_limit = max(1, limit) if isinstance(limit, int) and limit > 0 else _DEFAULT_SNAPSHOT_LIMIT
        resolved_depth = max(1, depth) if isinstance(depth, int) and depth > 0 else 12
        interactive_only = interactive if isinstance(interactive, bool) else True
        rows = await tab.page.evaluate(
            _SNAPSHOT_JS,
            {
                "depth": resolved_depth,
                "interactiveOnly": interactive_only,
                "limit": resolved_limit,
                "selector": selector,
            },
        )
        if not isinstance(rows, list):
            rows = []

        ref_entries: list[dict[str, Any]] = []
        tab.refs = {}
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            selector_value = _clean_str(row.get("selector"))
            if selector_value is None:
                continue
            ref_id = f"e{index}"
            tab.refs[ref_id] = selector_value
            ref_entries.append(
                {
                    "name": _clean_str(row.get("name")) or "",
                    "ref": ref_id,
                    "role": _clean_str(row.get("role")) or "",
                    "selector": selector_value,
                    "tag": _clean_str(row.get("tag")) or "",
                    "type": _clean_str(row.get("type")) or "",
                },
            )

        state.active_target_id = resolved_target_id
        title = await tab.page.title()
        if fmt == "aria":
            return {
                "action": "snapshot",
                "count": len(ref_entries),
                "format": "aria",
                "interactive": interactive_only,
                "mode": mode,
                "profile": profile_name,
                "refs": refs_mode or "role",
                "status": "ok",
                "targetId": resolved_target_id,
                "title": title,
                "url": tab.page.url,
                "elements": ref_entries,
            }

        lines: list[str] = [f"URL: {tab.page.url}", f"Title: {title}", "Elements:"]
        for entry in ref_entries:
            role_or_tag = entry["role"] or entry["tag"] or "element"
            name = entry["name"]
            base = f"[{entry['ref']}] {role_or_tag}"
            lines.append(f"{base}: {name}" if name else base)
        snapshot_text = "\n".join(lines if not compact else lines[:2] + lines[3:])
        resolved_max_chars = self._resolve_max_chars(max_chars=max_chars, mode=mode)
        if resolved_max_chars is not None and len(snapshot_text) > resolved_max_chars:
            snapshot_text = snapshot_text[:resolved_max_chars].rstrip() + "\n…"

        return {
            "action": "snapshot",
            "count": len(ref_entries),
            "format": "ai",
            "interactive": interactive_only,
            "mode": mode,
            "profile": profile_name,
            "refs": refs_mode or "role",
            "snapshot": snapshot_text,
            "status": "ok",
            "targetId": resolved_target_id,
            "title": title,
            "url": tab.page.url,
        }

    @staticmethod
    def _resolve_max_chars(*, max_chars: int | None, mode: str | None) -> int | None:
        if isinstance(max_chars, int):
            return max_chars if max_chars > 0 else None
        if mode == "efficient":
            return None
        return _DEFAULT_AI_SNAPSHOT_MAX_CHARS

    async def _act(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        *,
        profile_name: str,
        request: dict[str, Any],
        fallback_target_id: str | None,
    ) -> dict[str, Any]:
        state = await self._ensure_profile(profile_name)
        requested_target_id = _clean_str(request.get("targetId")) or fallback_target_id
        resolved_target_id, tab = await self._resolve_tab(state, requested_target_id)
        kind = _clean_str(request.get("kind"))
        if kind is None:
            msg = "request.kind required"
            raise ValueError(msg)

        if kind == "click":
            selector = self._resolve_selector(tab, _clean_str(request.get("ref")))
            if selector is None:
                msg = "click requires request.ref"
                raise ValueError(msg)
            click_kwargs: dict[str, Any] = {}
            if request.get("doubleClick") is True:
                click_kwargs["click_count"] = 2
            button = _clean_str(request.get("button"))
            if button is not None:
                click_kwargs["button"] = button
            modifiers = request.get("modifiers")
            if isinstance(modifiers, list):
                click_kwargs["modifiers"] = [str(modifier) for modifier in modifiers]
            await tab.page.locator(selector).first.click(**click_kwargs)
            return self._act_result(profile_name, resolved_target_id, kind)

        if kind == "type":
            selector = self._resolve_selector(tab, _clean_str(request.get("ref")))
            if selector is None:
                msg = "type requires request.ref"
                raise ValueError(msg)
            text = str(request.get("text", ""))
            locator = tab.page.locator(selector).first
            if request.get("slowly") is True:
                await locator.type(text, delay=75)
            else:
                await locator.fill(text)
            if request.get("submit") is True:
                await locator.press("Enter")
            return self._act_result(profile_name, resolved_target_id, kind, text=text)

        if kind == "press":
            key = _clean_str(request.get("key"))
            if key is None:
                msg = "press requires request.key"
                raise ValueError(msg)
            await tab.page.keyboard.press(key)
            return self._act_result(profile_name, resolved_target_id, kind, key=key)

        if kind == "hover":
            selector = self._resolve_selector(tab, _clean_str(request.get("ref")))
            if selector is None:
                msg = "hover requires request.ref"
                raise ValueError(msg)
            await tab.page.locator(selector).first.hover()
            return self._act_result(profile_name, resolved_target_id, kind)

        if kind == "drag":
            start_selector = self._resolve_selector(tab, _clean_str(request.get("startRef")))
            end_selector = self._resolve_selector(tab, _clean_str(request.get("endRef")))
            if start_selector is None or end_selector is None:
                msg = "drag requires request.startRef and request.endRef"
                raise ValueError(msg)
            await tab.page.drag_and_drop(start_selector, end_selector)
            return self._act_result(profile_name, resolved_target_id, kind)

        if kind == "select":
            selector = self._resolve_selector(tab, _clean_str(request.get("ref")))
            values = request.get("values")
            if selector is None or not isinstance(values, list) or not values:
                msg = "select requires request.ref and request.values"
                raise ValueError(msg)
            await tab.page.select_option(selector, [str(value) for value in values])
            return self._act_result(profile_name, resolved_target_id, kind, values=values)

        if kind == "fill":
            fields = request.get("fields")
            if not isinstance(fields, list) or not fields:
                msg = "fill requires request.fields"
                raise ValueError(msg)
            updated: list[dict[str, str]] = []
            for field in fields:
                if not isinstance(field, dict):
                    continue
                selector = self._resolve_selector(
                    tab,
                    _clean_str(field.get("ref")) or _clean_str(field.get("selector")),
                )
                if selector is None:
                    continue
                value = str(field.get("value", ""))
                await tab.page.locator(selector).first.fill(value)
                updated.append({"selector": selector, "value": value})
            if not updated:
                msg = "fill requires at least one field with a valid ref or selector"
                raise ValueError(msg)
            return self._act_result(profile_name, resolved_target_id, kind, fields=updated)

        if kind == "resize":
            width = request.get("width")
            height = request.get("height")
            if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
                msg = "resize requires positive integer request.width and request.height"
                raise ValueError(msg)
            await tab.page.set_viewport_size({"height": height, "width": width})
            return self._act_result(profile_name, resolved_target_id, kind, width=width, height=height)

        if kind == "wait":
            time_ms = request.get("timeMs")
            text = _clean_str(request.get("text"))
            text_gone = _clean_str(request.get("textGone"))
            if isinstance(time_ms, int) and time_ms >= 0:
                await tab.page.wait_for_timeout(time_ms)
            elif text is not None:
                await tab.page.wait_for_selector(f"text={text}", state="visible", timeout=_DEFAULT_TIMEOUT_MS)
            elif text_gone is not None:
                await tab.page.wait_for_selector(f"text={text_gone}", state="detached", timeout=_DEFAULT_TIMEOUT_MS)
            else:
                await tab.page.wait_for_timeout(500)
            return self._act_result(profile_name, resolved_target_id, kind)

        if kind == "evaluate":
            fn = _clean_str(request.get("fn"))
            if fn is None:
                msg = "evaluate requires request.fn"
                raise ValueError(msg)
            selector = self._resolve_selector(tab, _clean_str(request.get("ref")))
            result = await tab.page.evaluate(fn) if selector is None else await tab.page.eval_on_selector(selector, fn)
            return self._act_result(profile_name, resolved_target_id, kind, result=result)

        if kind == "close":
            await tab.page.close()
            self._remove_tab(state, resolved_target_id)
            return self._act_result(profile_name, resolved_target_id, kind)

        msg = f"Unsupported act kind: {kind}"
        raise ValueError(msg)

    @staticmethod
    def _act_result(profile_name: str, target_id: str, kind: str, **extra: object) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": "act",
            "kind": kind,
            "profile": profile_name,
            "status": "ok",
            "targetId": target_id,
        }
        payload.update(extra)
        return payload

    async def _ensure_profile(self, profile_name: str) -> _BrowserProfileState:
        async with self._lock:
            state = self._profiles.get(profile_name)
            if state is not None:
                return state

            playwright = await async_playwright().start()
            executable = (
                os.environ.get("BROWSER_EXECUTABLE_PATH")
                or shutil.which("chromium")
                or shutil.which("google-chrome-stable")
            )
            launch_kwargs: dict[str, Any] = {"headless": True}
            if executable:
                launch_kwargs["executable_path"] = executable
            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(viewport={"height": _VIEWPORT_HEIGHT, "width": _VIEWPORT_WIDTH})
            state = _BrowserProfileState(playwright=playwright, browser=browser, context=context)
            self._profiles[profile_name] = state

            page = await context.new_page()
            target_id = self._register_tab(state, page)
            state.active_target_id = target_id
            return state

    async def _stop_profile(self, profile_name: str) -> None:
        async with self._lock:
            state = self._profiles.pop(profile_name, None)
        if state is None:
            return
        await state.context.close()
        await state.browser.close()
        await state.playwright.stop()

    async def _resolve_tab(
        self,
        state: _BrowserProfileState,
        target_id: str | None,
    ) -> tuple[str, _BrowserTabState]:
        resolved_target_id = target_id or state.active_target_id
        if resolved_target_id is not None:
            tab = state.tabs.get(resolved_target_id)
            if tab is not None and not tab.page.is_closed():
                state.active_target_id = resolved_target_id
                return resolved_target_id, tab
        for candidate_id, tab in state.tabs.items():
            if not tab.page.is_closed():
                state.active_target_id = candidate_id
                return candidate_id, tab
        page = await state.context.new_page()
        candidate_id = self._register_tab(state, page)
        state.active_target_id = candidate_id
        return candidate_id, state.tabs[candidate_id]

    def _register_tab(self, state: _BrowserProfileState, page: Page) -> str:
        target_id = uuid4().hex[:8]
        tab = _BrowserTabState(target_id=target_id, page=page)
        state.tabs[target_id] = tab
        page.on("console", lambda message: self._record_console(tab, message))
        page.on("dialog", lambda dialog: asyncio.create_task(self._handle_dialog(tab, dialog)))
        page.on("close", lambda _: self._remove_tab(state, target_id))
        return target_id

    @staticmethod
    def _record_console(tab: _BrowserTabState, message: ConsoleMessage) -> None:
        entry = {
            "level": message.type,
            "location": message.location,
            "text": message.text,
        }
        tab.console.append(entry)
        if len(tab.console) > _MAX_CONSOLE_ENTRIES:
            del tab.console[:-_MAX_CONSOLE_ENTRIES]

    async def _handle_dialog(self, tab: _BrowserTabState, dialog: Dialog) -> None:
        behavior = tab.pending_dialog
        if behavior is None:
            await dialog.dismiss()
            return
        tab.pending_dialog = None
        if behavior.get("accept"):
            await dialog.accept(str(behavior.get("promptText") or ""))
            return
        await dialog.dismiss()

    @staticmethod
    def _resolve_selector(tab: _BrowserTabState, ref_or_selector: str | None) -> str | None:
        if ref_or_selector is None:
            return None
        return tab.refs.get(ref_or_selector, ref_or_selector)

    def _next_output_path(self, extension: str) -> Path:
        return self._output_dir / f"{uuid4().hex}.{extension}"

    @staticmethod
    def _remove_tab(state: _BrowserProfileState, target_id: str) -> None:
        state.tabs.pop(target_id, None)
        if state.active_target_id == target_id:
            state.active_target_id = next(iter(state.tabs.keys()), None)
