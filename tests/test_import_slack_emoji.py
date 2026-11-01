"""The Slack emoji importer copies a workspace's custom emoji into the server emoji packs and changes no existing emoji."""

from __future__ import annotations

import copy
import io
import json
import time
from typing import Any

import httpx
import pytest
from PIL import Image

from scripts.utilities.import_slack_emoji import (
    MAX_PACK_CONTENT_BYTES,
    add_to_packs,
    main,
    pack_content_bytes,
    plan_import,
)


def test_plan_splits_originals_and_aliases_and_skips_taken_and_unusable_names() -> None:
    """Images and aliases are imported under their Slack names; taken names and names Chat cannot use are not."""
    plan = plan_import(
        {
            "party-parrot": "https://emoji.slack-edge.com/T1/party-parrot/a.gif",
            "pp": "alias:party-parrot",
            "taken": "https://emoji.slack-edge.com/T1/taken/b.png",
            "bad name": "https://emoji.slack-edge.com/T1/x/c.png",
            "plain": "http://emoji.slack-edge.com/T1/plain/d.png",
        },
        taken={"taken"},
    )
    assert plan.originals == {"party-parrot": "https://emoji.slack-edge.com/T1/party-parrot/a.gif"}
    assert plan.aliases == {"pp": "party-parrot"}
    assert plan.taken == ["taken"]
    assert plan.unusable == ["bad name", "plain"]


def pack_with_free_bytes(free: int) -> dict[str, Any]:
    """Return a live pack whose content is exactly `free` bytes short of the size limit."""
    pack: dict[str, Any] = {
        "pack": {"display_name": "Emoji", "usage": ["emoticon"]},
        "images": {"padding": {"url": "mxc://example.com/"}},
    }
    pack["images"]["padding"]["url"] += "x" * (MAX_PACK_CONTENT_BYTES - free - pack_content_bytes(pack))
    return pack


def test_entries_fill_the_first_live_pack() -> None:
    """An image goes into the first pack as it is."""
    packs = {"": {"pack": {"display_name": "Emoji", "usage": ["emoticon"]}, "images": {}}}
    changed, skipped = add_to_packs(packs, {"a": {"url": "mxc://example.com/a"}})
    assert changed == [""]
    assert skipped == []
    assert packs[""]["images"] == {"a": {"url": "mxc://example.com/a"}}


def test_a_full_pack_overflows_into_pack_2_with_the_first_pack_metadata() -> None:
    """An image one byte too big for the first pack opens pack "2" with the first pack's name and usage."""
    entry = {"url": "mxc://example.com/new"}
    packs = {"": pack_with_free_bytes(len(',"new":{"url":"mxc://example.com/new"}') - 1)}
    first = copy.deepcopy(packs[""])
    changed, skipped = add_to_packs(packs, {"new": entry})
    assert changed == ["2"]
    assert skipped == []
    assert packs["2"] == {"pack": packs[""]["pack"], "images": {"new": entry}}
    assert packs[""] == first


def test_a_deleted_first_pack_is_skipped_and_overflow_starts_after_live_packs() -> None:
    """Images go into the live pack "2" and then a new pack "3"; the deleted first pack stays deleted."""
    one = {"url": "mxc://example.com/one"}
    two = {"url": "mxc://example.com/two"}
    packs = {"": {}, "2": pack_with_free_bytes(len(',"one":{"url":"mxc://example.com/one"}'))}
    changed, _ = add_to_packs(packs, {"one": one, "two": two})
    assert changed == ["2", "3"]
    assert packs["2"]["images"]["one"] == one
    assert packs["3"] == {"pack": packs["2"]["pack"], "images": {"two": two}}
    assert packs[""] == {}


def test_a_sticker_only_pack_marks_imported_entries_as_emoticons() -> None:
    """An image added to a sticker pack is marked as an emoji, so the picker shows it among the emoji."""
    packs = {"": {"pack": {"usage": ["sticker"]}, "images": {}}}
    add_to_packs(packs, {"a": {"url": "mxc://example.com/a"}})
    assert packs[""]["images"]["a"] == {"url": "mxc://example.com/a", "usage": ["emoticon"]}


@pytest.mark.parametrize("packs", [{}, {"": {}, "2": {}}], ids=["no packs", "only deleted packs"])
def test_no_live_pack_starts_the_first_pack(packs: dict[str, dict[str, Any]]) -> None:
    """Without a live pack, the image starts the first pack, without metadata, as MindRoom Chat does."""
    changed, _ = add_to_packs(packs, {"a": {"url": "mxc://example.com/a"}})
    assert changed == [""]
    assert packs[""] == {"images": {"a": {"url": "mxc://example.com/a"}}}


def test_names_taken_in_any_live_pack_are_skipped() -> None:
    """A name another pack has keeps its image there, and the other names are still added."""
    packs = {
        "": {"images": {"a": {"url": "mxc://example.com/a"}}},
        "2": {"images": {"b": {"url": "mxc://example.com/b"}}},
    }
    changed, skipped = add_to_packs(packs, {"b": {"url": "mxc://example.com/new"}, "c": {"url": "mxc://example.com/c"}})
    assert skipped == ["b"]
    assert changed == [""]
    assert packs["2"]["images"]["b"] == {"url": "mxc://example.com/b"}
    assert "c" in packs[""]["images"]


def test_multibyte_names_count_in_utf8_bytes() -> None:
    """A name that would fit by its characters but not by its UTF-8 bytes goes to a new pack; an exact fit stays."""
    assert pack_content_bytes({"images": {"é": {"url": "u"}}}) == len('{"images":{"é":{"url":"u"}}}'.encode())
    image = {"url": "mxc://example.com/a"}
    # "é" is one character in two bytes, so it fills exactly the free bytes.
    free = len(',"é":{"url":"mxc://example.com/a"}'.encode())
    exact = {"": pack_with_free_bytes(free)}
    assert add_to_packs(exact, {"é": image}) == ([""], [])
    # "絵" is one character too, but in three bytes.
    over = {"": pack_with_free_bytes(free)}
    assert add_to_packs(over, {"絵": image}) == (["2"], [])


def image_bytes(image_format: str, size: int) -> bytes:
    """Return a square image file in the format, as Slack's CDN serves custom emoji."""
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), "orange").save(buffer, image_format)
    return buffer.getvalue()


PNG = image_bytes("PNG", 64)
GIF = image_bytes("GIF", 32)
CDN = "https://emoji.slack-edge.com/T1"
SLACK_TOKEN = "xoxb-slack-secret"  # noqa: S105
MATRIX_TOKEN = "matrix-secret"  # noqa: S105
HOMESERVER = ["--homeserver", "https://matrix.example.com"]
ROOM = "/_matrix/client/v3/rooms/!emoji:example.com"
PACK_STATE = f"{ROOM}/state/im.ponies.room_emotes/"
RATE_LIMITED = {"errcode": "M_LIMIT_EXCEEDED", "error": "Too many requests", "retry_after_ms": 1}
# Tuwunel refuses an event over 65,535 bytes, counting the content it replaces, which it keeps in the event as
# unsigned.prev_content; IDs, hashes, and signatures around the content take up to about a kilobyte of that.
EVENT_CONTENT_LIMIT_BYTES = 65_535 - 1_000


class Servers:
    """Answer Slack's API, Slack's image CDN, and the homeserver, and keep what the script sent them."""

    def __init__(self) -> None:
        self.emoji = {
            "party-parrot": f"{CDN}/party-parrot/a.gif",
            "pp": "alias:party-parrot",
            "shipit": f"{CDN}/shipit/b.png",
            "thumbsup": "alias:+1",
        }
        self.slack_answer: dict[str, Any] | None = None
        self.files: dict[str, tuple[int, str, bytes]] = {
            f"{CDN}/party-parrot/a.gif": (200, "image/gif", GIF),
            f"{CDN}/shipit/b.png": (200, "image/png", PNG),
        }
        # The pack the room script creates.
        self.packs: dict[str, dict[str, Any]] = {
            "": {"pack": {"display_name": "Emoji", "usage": ["emoticon"]}, "images": {}},
        }
        self.authorization: dict[str, set[str | None]] = {}
        self.joined: list[str] = []
        self.downloads: list[str] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.writes: list[str] = []
        self.rate_limit_once: set[str] = set()
        self.added_in_chat_during_upload: dict[str, dict[str, Any]] = {}
        self.refuse_writes = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Route each request to the service its host names, noting which token it carried."""
        self.authorization.setdefault(request.url.host, set()).add(request.headers.get("Authorization"))
        if request.url.host == "slack.com" and request.url.path == "/api/emoji.list":
            return httpx.Response(200, json=self.slack_answer or {"ok": True, "emoji": self.emoji})
        if request.url.host == "emoji.slack-edge.com":
            self.downloads.append(str(request.url))
            status, content_type, content = self.files[str(request.url)]
            return httpx.Response(status, headers={"Content-Type": content_type}, content=content)
        if request.url.host == "matrix.example.com":
            return self.matrix(request)
        message = f"Unexpected request: {request.method} {request.url}"
        raise AssertionError(message)

    def matrix(self, request: httpx.Request) -> httpx.Response:
        """Implement the operator's identity, joining, and room state, and hand on uploads and pack writes."""
        method, path = request.method, request.url.path
        if method == "GET" and path == "/_matrix/client/v3/account/whoami":
            return httpx.Response(200, json={"user_id": "@op:example.com"})
        if method == "POST" and path.startswith("/_matrix/client/v3/join/"):
            self.joined.append(path.removeprefix("/_matrix/client/v3/join/"))
            return httpx.Response(200, json={"room_id": "!emoji:example.com"})
        if method == "GET" and path == f"{ROOM}/state":
            packs = [
                {"type": "im.ponies.room_emotes", "state_key": key, "content": pack} for key, pack in self.packs.items()
            ]
            return httpx.Response(200, json=[{"type": "m.room.create", "state_key": "", "content": {}}, *packs])
        if method == "POST" and path == "/_matrix/media/v3/upload":
            return self.upload(request)
        if method == "PUT" and path.startswith(PACK_STATE):
            return self.write(path.removeprefix(PACK_STATE), json.loads(request.content))
        message = f"Unexpected Matrix request: {method} {path}"
        raise AssertionError(message)

    def upload(self, request: httpx.Request) -> httpx.Response:
        """Keep an uploaded image, while someone may be adding emoji in MindRoom Chat."""
        if self.rate_limited("upload"):
            return httpx.Response(429, json=RATE_LIMITED)
        self.packs[""]["images"].update(self.added_in_chat_during_upload)
        self.uploads.append((request.headers["Content-Type"], request.content))
        return httpx.Response(200, json={"content_uri": f"mxc://example.com/{len(self.uploads)}"})

    def write(self, state_key: str, content: dict[str, Any]) -> httpx.Response:
        """Replace the pack at the state key, unless the account may not change emoji or the event gets too big."""
        if self.refuse_writes:
            return httpx.Response(403, json={"errcode": "M_FORBIDDEN", "error": "Not allowed to change emoji"})
        if self.rate_limited("write"):
            return httpx.Response(429, json=RATE_LIMITED)
        event = {"content": content, "unsigned": {"prev_content": self.packs.get(state_key, {})}}
        if len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()) > EVENT_CONTENT_LIMIT_BYTES:
            return httpx.Response(413, json={"errcode": "M_TOO_LARGE", "error": "PDU is larger than maximum"})
        self.packs[state_key] = content
        self.writes.append(state_key)
        return httpx.Response(200, json={"event_id": "$pack"})

    def rate_limited(self, kind: str) -> bool:
        """Whether to answer this request of the kind with a rate limit; only the first one gets it."""
        if kind not in self.rate_limit_once:
            return False
        self.rate_limit_once.remove(kind)
        return True


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> Servers:
    """Point `main` at the fake services and give it both tokens."""
    fake = Servers()
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(fake.handle), **kwargs),
    )
    monkeypatch.setenv("SLACK_TOKEN", SLACK_TOKEN)
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", MATRIX_TOKEN)
    return fake


def test_imports_every_custom_emoji_with_its_slack_name_and_image_info(
    servers: Servers,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each image is uploaded as Slack serves it and added to the first pack under its Slack name, with size and type."""
    assert main(HOMESERVER) == 0
    assert servers.joined == ["#emoji:example.com"]
    assert servers.uploads == [("image/gif", GIF), ("image/png", PNG)]
    pack = servers.packs[""]
    assert pack["pack"] == {"display_name": "Emoji", "usage": ["emoticon"]}
    assert pack["images"]["party-parrot"] == {
        "url": "mxc://example.com/1",
        "info": {"w": 32, "h": 32, "mimetype": "image/gif", "size": len(GIF)},
    }
    assert pack["images"]["shipit"] == {
        "url": "mxc://example.com/2",
        "info": {"w": 64, "h": 64, "mimetype": "image/png", "size": len(PNG)},
    }
    assert capsys.readouterr().out == "Imported 2 emoji and 1 aliases; skipped 0 taken, 1 unusable, 0 failed.\n"


def test_an_alias_shares_its_target_image_and_a_standard_emoji_alias_is_skipped(
    servers: Servers,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An alias names its target's uploaded image again; an alias of a standard emoji has no image and counts as unusable."""
    assert main(HOMESERVER) == 0
    images = servers.packs[""]["images"]
    assert images["pp"] == images["party-parrot"]
    assert "thumbsup" not in images
    assert len(servers.uploads) == 2
    assert capsys.readouterr().out == "Imported 2 emoji and 1 aliases; skipped 0 taken, 1 unusable, 0 failed.\n"


def test_a_second_run_uploads_and_writes_nothing(servers: Servers, capsys: pytest.CaptureFixture[str]) -> None:
    """Every name is taken after the first run, so the second downloads, uploads, and writes nothing."""
    assert main(HOMESERVER) == 0
    imported = copy.deepcopy(servers.packs)
    servers.downloads.clear()
    servers.uploads.clear()
    servers.writes.clear()
    capsys.readouterr()
    assert main(HOMESERVER) == 0
    assert (servers.downloads, servers.uploads, servers.writes) == ([], [], [])
    assert servers.packs == imported
    assert capsys.readouterr().out == "Imported 0 emoji and 0 aliases; skipped 3 taken, 1 unusable, 0 failed.\n"


def test_a_name_taken_between_planning_and_writing_is_kept(
    servers: Servers,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An emoji someone adds in MindRoom Chat while the import runs keeps its image, and the others are still added."""
    someone_elses = {"url": "mxc://example.com/someone-elses"}
    servers.added_in_chat_during_upload = {"party-parrot": someone_elses}
    assert main(HOMESERVER) == 0
    assert servers.packs[""]["images"]["party-parrot"] == someone_elses
    assert servers.packs[""]["images"]["shipit"]["url"] == "mxc://example.com/2"
    assert capsys.readouterr().out == "Imported 1 emoji and 1 aliases; skipped 1 taken, 1 unusable, 0 failed.\n"


def test_dry_run_uploads_and_writes_nothing_and_prints_counts(
    servers: Servers,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dry run joins the given room to read its packs, then only reports what a real run would import."""
    assert main([*HOMESERVER, "--room", "#stickers:example.com", "--dry-run"]) == 0
    assert servers.joined == ["#stickers:example.com"]
    assert (servers.downloads, servers.uploads, servers.writes) == ([], [], [])
    assert capsys.readouterr().out == "Would import 2 emoji and 1 aliases; skipped 0 taken, 1 unusable.\n"


def test_a_failed_download_is_counted_and_the_run_continues(
    servers: Servers,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An emoji whose image cannot be fetched or read is skipped and named, and the other emoji are imported."""
    servers.emoji |= {
        "forbidden": f"{CDN}/forbidden/c.png",
        "html": f"{CDN}/html/d.png",
        "broken": f"{CDN}/broken/e.png",
    }
    servers.files |= {
        f"{CDN}/forbidden/c.png": (403, "text/plain", b"Forbidden"),
        f"{CDN}/html/d.png": (200, "text/html", b"<html>Sign in</html>"),
        f"{CDN}/broken/e.png": (200, "image/png", b"not a PNG"),
    }
    assert main(HOMESERVER) == 0
    assert sorted(servers.packs[""]["images"]) == ["party-parrot", "pp", "shipit"]
    out, err = capsys.readouterr()
    assert out == "Imported 2 emoji and 1 aliases; skipped 0 taken, 1 unusable, 3 failed.\n"
    # Only the script's own lines count: logging another test left configured may write to stderr too.
    assert [line.partition(" (")[0] for line in err.splitlines() if line.startswith("Could not import ")] == [
        "Could not import broken",
        "Could not import forbidden",
        "Could not import html",
    ]
    assert "download answered 403 with text/plain" in err
    assert "download answered 200 with text/html" in err


def test_rate_limited_upload_and_write_are_retried(
    servers: Servers,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script waits as long as the homeserver asks and sends the upload and the pack write again."""
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    servers.rate_limit_once = {"upload", "write"}
    assert main(HOMESERVER) == 0
    assert waits == [0.001, 0.001]
    assert servers.uploads == [("image/gif", GIF), ("image/png", PNG)]
    assert sorted(servers.packs[""]["images"]) == ["party-parrot", "pp", "shipit"]
    assert capsys.readouterr().out == "Imported 2 emoji and 1 aliases; skipped 0 taken, 1 unusable, 0 failed.\n"


def test_slack_error_stops_before_any_upload(servers: Servers, capsys: pytest.CaptureFixture[str]) -> None:
    """A Slack token without the emoji:read scope stops the run with Slack's error code."""
    servers.slack_answer = {"ok": False, "error": "missing_scope"}
    assert main(HOMESERVER) == 1
    assert "Import stopped: Slack answered missing_scope" in capsys.readouterr().err.splitlines()
    assert (servers.downloads, servers.uploads, servers.writes) == ([], [], [])


def test_a_big_workspace_fills_packs_the_homeserver_accepts(
    servers: Servers,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each pack stays small enough that a write fits in one event together with the content it replaces."""
    for number in range(500):
        url = f"{CDN}/emoji-{number:03d}/x.png"
        servers.emoji[f"emoji-{number:03d}"] = url
        servers.files[url] = (200, "image/png", PNG)
    assert main(HOMESERVER) == 0
    assert sorted(servers.packs) == ["", "2"]
    assert sum(len(pack["images"]) for pack in servers.packs.values()) == 503
    assert capsys.readouterr().out == "Imported 502 emoji and 1 aliases; skipped 0 taken, 1 unusable, 0 failed.\n"


@pytest.mark.parametrize("variable", ["SLACK_TOKEN", "MATRIX_ACCESS_TOKEN"])
def test_missing_tokens_exit_with_an_error(
    servers: Servers,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    """Without either token the script names the missing variable and sends nothing."""
    monkeypatch.delenv(variable)
    with pytest.raises(SystemExit) as stopped:
        main(HOMESERVER)
    assert stopped.value.code == 2
    assert variable in capsys.readouterr().err
    assert servers.authorization == {}


def test_tokens_are_never_printed(servers: Servers, capsys: pytest.CaptureFixture[str]) -> None:
    """Neither token appears in the output, even on errors, and each goes only to its own service."""
    servers.emoji["forbidden"] = f"{CDN}/forbidden/c.png"
    servers.files[f"{CDN}/forbidden/c.png"] = (403, "text/plain", b"Forbidden")
    servers.refuse_writes = True
    assert main(HOMESERVER) == 1
    out, err = capsys.readouterr()
    assert "forbidden" in err
    assert "M_FORBIDDEN" in err
    assert SLACK_TOKEN not in out + err
    assert MATRIX_TOKEN not in out + err
    assert servers.authorization == {
        "matrix.example.com": {f"Bearer {MATRIX_TOKEN}"},
        "slack.com": {f"Bearer {SLACK_TOKEN}"},
        "emoji.slack-edge.com": {None},
    }
