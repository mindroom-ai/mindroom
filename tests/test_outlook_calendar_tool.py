# ruff: noqa: D103
"""Tests for the Outlook calendar toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.tools.function import Function

from mindroom.custom_tools import outlook_calendar
from mindroom.custom_tools.outlook_calendar import OutlookCalendarTools
from mindroom.oauth.microsoft import outlook_calendar_oauth_provider
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.declarations import SetupType, ToolFileAccess
from tests.microsoft_graph_test_support import (
    FakeGraph,
    publish_grant,
    runtime_paths,
    save_client_config,
    worker_target,
)

if TYPE_CHECKING:
    from pathlib import Path

EVENT_ID = "AAMkAGI2Ev-1_x="
CALENDAR_ID = "AAMkCal+1="
WRITE_FUNCTIONS = {
    "outlook_calendar_create_event",
    "outlook_calendar_update_event",
    "outlook_calendar_delete_event",
    "outlook_calendar_respond_to_event",
}


def _graph_event(**overrides: object) -> dict[str, object]:
    return {
        "id": EVENT_ID,
        "subject": "Design review",
        "start": {"dateTime": "2026-10-21T16:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-21T17:00:00.0000000", "timeZone": "UTC"},
        "isAllDay": False,
        "location": {"displayName": "Room 4"},
        "organizer": {"emailAddress": {"name": "Alex Example", "address": "alex@contoso.com"}},
        "attendees": [
            {
                "type": "required",
                "status": {"response": "accepted", "time": "2026-10-20T10:00:00Z"},
                "emailAddress": {"name": "Sam Example", "address": "sam@contoso.com"},
            },
        ],
        "isOnlineMeeting": True,
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/example"},
        "showAs": "busy",
        "webLink": "https://outlook.office365.com/owa/?itemid=example",
        "bodyPreview": "Agenda",
        "isOrganizer": False,
        "responseStatus": {"response": "notResponded"},
        "isCancelled": False,
        "type": "singleInstance",
        **overrides,
    }


EVENT_SUMMARY = {
    "id": EVENT_ID,
    "subject": "Design review",
    "start": "2026-10-21T16:00:00Z",
    "end": "2026-10-21T17:00:00Z",
    "is_all_day": False,
    "location": "Room 4",
    "organizer": {"name": "Alex Example", "email": "alex@contoso.com"},
    "attendees": [{"name": "Sam Example", "email": "sam@contoso.com", "response": "accepted"}],
    "attendee_count": 1,
    "is_online_meeting": True,
    "join_url": "https://teams.microsoft.com/l/meetup-join/example",
    "show_as": "busy",
    "web_link": "https://outlook.office365.com/owa/?itemid=example",
    "body_preview": "Agenda",
    "is_organizer": False,
    "my_response": "notResponded",
    "is_cancelled": False,
    "event_type": "singleInstance",
}


class _Graph:
    """Answer requests by method and decoded path; anything else is a Graph 404."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response] | None = None) -> None:
        self.routes = routes or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        answer = self.routes.get((request.method, request.url.path.removeprefix("/v1.0")))
        return answer or httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})


def _tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[tuple[str, str], httpx.Response] | None = None,
    *,
    write: bool = True,
) -> tuple[OutlookCalendarTools, FakeGraph]:
    graph = FakeGraph(_Graph(routes)).install(monkeypatch)
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(outlook_calendar_oauth_provider(), manager)
    tools = OutlookCalendarTools(
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=worker_target(),
        runtime_config=None,
        write=write,
    )
    return tools, graph


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def _description(tools: OutlookCalendarTools, name: str) -> str:
    """Return the description the model sees; deferred toolkits do not show toolkit instructions."""
    return " ".join(Function.from_callable(getattr(tools, name)).to_dict()["description"].split())


def test_registered_metadata_declares_a_primary_runtime_oauth_tool() -> None:
    metadata = TOOL_METADATA["outlook_calendar"]

    assert metadata.display_name == "Outlook Calendar"
    assert metadata.auth_provider == "outlook_calendar"
    assert metadata.setup_type == SetupType.OAUTH
    assert metadata.requires_primary_runtime is True
    assert metadata.file_access == ToolFileAccess.NONE
    assert metadata.consumes_workspace_paths is False
    assert metadata.function_names == outlook_calendar.FUNCTION_NAMES


def test_provider_requests_only_calendar_read_write() -> None:
    provider = outlook_calendar_oauth_provider()

    assert provider.id == "outlook_calendar"
    assert provider.display_name == "Outlook Calendar"
    assert provider.scopes == ("Calendars.ReadWrite", "User.Read", "offline_access")
    assert provider.credential_service == "outlook_calendar_oauth"
    assert provider.shared_client_config_services == ("microsoft_oauth_client",)
    assert provider.requester_scoped_credentials is True


def test_write_false_registers_only_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, write=False)

    assert set(tools.functions) == set(outlook_calendar.FUNCTION_NAMES) - WRITE_FUNCTIONS


def test_write_true_registers_every_function(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch)

    assert set(tools.functions) == set(outlook_calendar.FUNCTION_NAMES)


def test_descriptions_state_utc_and_make_writes_legible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch)

    assert "Times in the result are UTC" in _description(tools, "outlook_calendar_list_events")
    assert "Times in the result are UTC" in _description(tools, "outlook_calendar_get_event")
    for name in WRITE_FUNCTIONS:
        assert "Before calling, tell the requester" in _description(tools, name), name
    assert "Outlook sends every attendee a cancellation" in _description(tools, "outlook_calendar_delete_event")
    assert "Outlook emails an invitation to every attendee" in _description(tools, "outlook_calendar_create_event")


def test_list_calendars_summarizes_each_calendar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calendar = {
        "id": CALENDAR_ID,
        "name": "Calendar",
        "owner": {"name": "Alex Example", "address": "alex@contoso.com"},
        "canEdit": True,
        "isDefaultCalendar": True,
    }
    routes = {("GET", "/me/calendars"): httpx.Response(200, json={"value": [calendar], "@odata.nextLink": "x"})}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_list_calendars())

    assert result["calendars"] == [
        {
            "id": CALENDAR_ID,
            "name": "Calendar",
            "owner": {"name": "Alex Example", "email": "alex@contoso.com"},
            "can_edit": True,
            "is_default": True,
        },
    ]
    assert result["more_available"] is True
    assert dict(graph.requests[0].url.params) == {"$select": "id,name,owner,canEdit,isDefaultCalendar", "$top": "100"}


def test_list_events_reads_the_default_calendar_view_in_utc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {("GET", "/me/calendar/calendarView"): httpx.Response(200, json={"value": [_graph_event()]})}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_list_events("2026-10-21T00:00:00+02:00", "2026-10-22", limit=10))

    assert result == {"status": "ok", "tool": "outlook_calendar", "events": [EVENT_SUMMARY], "more_available": False}
    [request] = graph.requests
    assert request.headers["prefer"] == 'outlook.timezone="UTC"'
    assert dict(request.url.params) == {
        "startDateTime": "2026-10-20T22:00:00Z",
        "endDateTime": "2026-10-22T00:00:00Z",
        "$select": outlook_calendar._EVENT_SELECT,
        "$orderby": "start/dateTime",
        "$top": "10",
    }


def test_list_events_escapes_a_calendar_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {
        ("GET", f"/me/calendars/{CALENDAR_ID}/calendarView"): httpx.Response(
            200,
            json={"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"},
        ),
    }
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_list_events("2026-10-21", "2026-10-22", calendar=CALENDAR_ID))

    assert result["more_available"] is True
    assert graph.requests[0].url.raw_path.split(b"?")[0] == b"/v1.0/me/calendars/AAMkCal%2B1=/calendarView"


def test_event_summary_caps_attendees_and_keeps_a_foreign_time_zone_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attendees = [{"emailAddress": {"address": f"p{index}@contoso.com"}} for index in range(150)]
    event = _graph_event(attendees=attendees, start={"dateTime": "2026-10-21T09:00:00", "timeZone": "Europe/Berlin"})
    routes = {("GET", "/me/calendar/calendarView"): httpx.Response(200, json={"value": [event]})}
    tools, _graph = _tools(tmp_path, monkeypatch, routes)

    [summary] = _payload(tools.outlook_calendar_list_events("2026-10-21", "2026-10-22"))["events"]

    assert len(summary["attendees"]) == outlook_calendar.MAX_ATTENDEES
    assert summary["attendee_count"] == 150
    assert summary["start"] == "2026-10-21T09:00:00 (Europe/Berlin)"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"start": "tomorrow", "end": "2026-10-22"}, "start must be an ISO 8601"),
        ({"start": "2026-10-22", "end": "2026-10-21"}, "end must be after start"),
        ({"start": "2026-10-21", "end": "2026-10-21"}, "end must be after start"),
        ({"start": "2026-01-01", "end": "2027-01-03"}, "at most 366 days"),
        ({"start": "2026-10-21", "end": "2026-10-22", "limit": 0}, "limit must be"),
        ({"start": "2026-10-21", "end": "2026-10-22", "limit": 51}, "limit must be"),
        ({"start": "2026-10-21", "end": "2026-10-22", "limit": True}, "limit must be"),
        ({"start": "2026-10-21", "end": "2026-10-22", "calendar": "a/b"}, "calendar must be"),
        ({"start": "2026-10-21", "end": "2026-10-22", "calendar": ".."}, "calendar must be"),
    ],
)
def test_list_events_refuses_bad_arguments_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch)

    result = _payload(tools.outlook_calendar_list_events(**kwargs))

    assert result["code"] == "invalid_argument"
    assert message in result["message"]
    assert graph.requests == []


def test_get_event_returns_the_text_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event = _graph_event(body={"contentType": "text", "content": "Agenda\n1. Plan"})
    routes = {("GET", f"/me/events/{EVENT_ID}"): httpx.Response(200, json=event)}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_get_event(EVENT_ID))

    assert result["event"] == {**EVENT_SUMMARY, "body": "Agenda\n1. Plan", "truncated": False}
    [request] = graph.requests
    assert request.headers["prefer"] == 'outlook.timezone="UTC", outlook.body-content-type="text"'
    assert request.url.params["$select"] == f"{outlook_calendar._EVENT_SELECT},body"


def test_get_event_truncates_a_long_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = "x" * (outlook_calendar.MAX_BODY_CHARACTERS + 5)
    routes = {("GET", f"/me/events/{EVENT_ID}"): httpx.Response(200, json=_graph_event(body={"content": content}))}
    tools, _graph = _tools(tmp_path, monkeypatch, routes)

    event = _payload(tools.outlook_calendar_get_event(EVENT_ID))["event"]

    assert len(event["body"]) == outlook_calendar.MAX_BODY_CHARACTERS
    assert event["truncated"] is True


@pytest.mark.parametrize("event_id", ["a/b", "a?b", "a#b", "..", "", "  "])
def test_an_event_id_that_changes_the_path_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_id: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch)

    assert _payload(tools.outlook_calendar_get_event(event_id))["code"] == "invalid_argument"
    assert _payload(tools.outlook_calendar_delete_event(event_id))["code"] == "invalid_argument"
    assert _payload(tools.outlook_calendar_respond_to_event(event_id, "accept"))["code"] == "invalid_argument"
    assert _payload(tools.outlook_calendar_update_event(event_id, subject="x"))["code"] == "invalid_argument"
    assert graph.requests == []


def test_graph_error_codes_pass_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch)

    result = _payload(tools.outlook_calendar_get_event(EVENT_ID))

    assert (result["status"], result["code"], result["graph_code"]) == ("error", "not_found", "ErrorItemNotFound")


def test_create_event_posts_local_times_with_their_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {("POST", "/me/calendar/events"): httpx.Response(201, json=_graph_event())}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(
        tools.outlook_calendar_create_event(
            "Design review",
            "2026-10-21T18:00:00",
            "2026-10-21T19:00",
            time_zone="W. Europe Standard Time",
            attendees=["sam@contoso.com", " kim@example.org "],
            location="Room 4",
            body="Agenda",
            is_online_meeting=True,
        ),
    )

    assert result["event"] == EVENT_SUMMARY
    [request] = graph.requests
    assert request.headers["prefer"] == 'outlook.timezone="UTC"'
    assert _body(request) == {
        "subject": "Design review",
        "start": {"dateTime": "2026-10-21T18:00:00", "timeZone": "W. Europe Standard Time"},
        "end": {"dateTime": "2026-10-21T19:00:00", "timeZone": "W. Europe Standard Time"},
        "attendees": [
            {"emailAddress": {"address": "sam@contoso.com"}, "type": "required"},
            {"emailAddress": {"address": "kim@example.org"}, "type": "required"},
        ],
        "location": {"displayName": "Room 4"},
        "body": {"contentType": "text", "content": "Agenda"},
        "isOnlineMeeting": True,
    }


def test_create_event_in_another_calendar_sends_only_given_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = {("POST", f"/me/calendars/{CALENDAR_ID}/events"): httpx.Response(201, json=_graph_event())}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    tools.outlook_calendar_create_event(
        "Focus",
        "2026-10-21T09:00:00",
        "2026-10-21T10:00:00",
        calendar=CALENDAR_ID,
        time_zone="America/Port-au-Prince",
    )

    assert _body(graph.requests[0]) == {
        "subject": "Focus",
        "start": {"dateTime": "2026-10-21T09:00:00", "timeZone": "America/Port-au-Prince"},
        "end": {"dateTime": "2026-10-21T10:00:00", "timeZone": "America/Port-au-Prince"},
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"subject": "  "}, "subject must not be empty"),
        ({"start": "2026-10-21T09:00:00Z"}, "without an offset"),
        ({"end": "2026-10-21T10:00:00+02:00"}, "without an offset"),
        ({"end": "2026-10-21T08:00:00"}, "end must be after start"),
        ({"start": "next week"}, "start must be an ISO 8601"),
        ({"time_zone": "Europe/Berlin'; DROP"}, "time_zone must be"),
        ({"time_zone": " UTC"}, "time_zone must be"),
        ({"time_zone": ""}, "time_zone must be"),
        ({"attendees": ["not-an-email"]}, "attendees must be"),
        ({"attendees": ["a@contoso.com, b@contoso.com"]}, "attendees must be"),
        ({"attendees": "a@contoso.com"}, "attendees must be"),
        ({"attendees": [f"p{index}@contoso.com" for index in range(101)]}, "at most 100"),
        ({"calendar": "a/b"}, "calendar must be"),
        ({"location": 5}, "location must be text"),
        ({"is_online_meeting": "yes"}, "is_online_meeting must be"),
    ],
)
def test_create_event_refuses_bad_arguments_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch)
    arguments = {"subject": "Sync", "start": "2026-10-21T09:00:00", "end": "2026-10-21T10:00:00", **kwargs}

    result = _payload(tools.outlook_calendar_create_event(**arguments))

    assert result["code"] == "invalid_argument"
    assert message in result["message"]
    assert graph.requests == []


def test_update_event_patches_only_the_given_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {("PATCH", f"/me/events/{EVENT_ID}"): httpx.Response(200, json=_graph_event(subject="Moved"))}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(
        tools.outlook_calendar_update_event(
            EVENT_ID,
            subject="Moved",
            start="2026-10-22T09:00:00",
            time_zone="Pacific Standard Time",
            location="",
        ),
    )

    assert result["event"]["subject"] == "Moved"
    [request] = graph.requests
    assert request.method == "PATCH"
    assert _body(request) == {
        "subject": "Moved",
        "start": {"dateTime": "2026-10-22T09:00:00", "timeZone": "Pacific Standard Time"},
        "location": {"displayName": ""},
    }


def test_update_event_refuses_when_nothing_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch)

    result = _payload(tools.outlook_calendar_update_event(EVENT_ID, time_zone="Pacific Standard Time"))

    assert result["code"] == "invalid_argument"
    assert graph.requests == []


def test_update_event_refuses_an_end_before_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch)

    result = _payload(tools.outlook_calendar_update_event(EVENT_ID, start="2026-10-22T10:00", end="2026-10-22T09:00"))

    assert result["message"] == "end must be after start."
    assert graph.requests == []


def test_update_event_never_replaces_an_online_meeting_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {("GET", f"/me/events/{EVENT_ID}"): httpx.Response(200, json={"isOnlineMeeting": True})}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_update_event(EVENT_ID, body="New agenda"))

    assert result["code"] == "online_meeting_body"
    assert [request.method for request in graph.requests] == ["GET"]
    assert graph.requests[0].url.params["$select"] == "isOnlineMeeting"


def test_update_event_replaces_the_body_of_an_ordinary_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {
        ("GET", f"/me/events/{EVENT_ID}"): httpx.Response(200, json={"isOnlineMeeting": False}),
        ("PATCH", f"/me/events/{EVENT_ID}"): httpx.Response(200, json=_graph_event(isOnlineMeeting=False)),
    }
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_update_event(EVENT_ID, body="New agenda"))

    assert result["status"] == "ok"
    assert [request.method for request in graph.requests] == ["GET", "PATCH"]
    assert _body(graph.requests[1]) == {"body": {"contentType": "text", "content": "New agenda"}}


def test_delete_event_deletes_the_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {("DELETE", f"/me/events/{EVENT_ID}"): httpx.Response(204)}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_delete_event(EVENT_ID))

    assert result == {"status": "ok", "tool": "outlook_calendar", "deleted": True, "event_id": EVENT_ID}
    assert graph.calls() == [f"DELETE graph.microsoft.com/v1.0/me/events/{EVENT_ID}"]


@pytest.mark.parametrize(
    ("response", "action"),
    [("accept", "accept"), ("tentative", "tentativelyAccept"), ("decline", "decline")],
)
def test_respond_to_event_posts_the_matching_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    action: str,
) -> None:
    routes = {("POST", f"/me/events/{EVENT_ID}/{action}"): httpx.Response(202)}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_respond_to_event(EVENT_ID, response, comment="See you"))  # type: ignore[arg-type]

    assert result == {
        "status": "ok",
        "tool": "outlook_calendar",
        "event_id": EVENT_ID,
        "response": response,
        "response_sent": True,
    }
    assert _body(graph.requests[0]) == {"sendResponse": True, "comment": "See you"}


def test_respond_without_sending_omits_the_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {("POST", f"/me/events/{EVENT_ID}/decline"): httpx.Response(202)}
    tools, graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_respond_to_event(EVENT_ID, "decline", send_response=False))

    assert result["response_sent"] is False
    assert _body(graph.requests[0]) == {"sendResponse": False}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"response": "maybe"},
        {"response": "accept", "comment": "Sorry", "send_response": False},
        {"response": "accept", "send_response": "yes"},
    ],
)
def test_respond_refuses_bad_arguments_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch)

    assert _payload(tools.outlook_calendar_respond_to_event(EVENT_ID, **kwargs))["code"] == "invalid_argument"
    assert graph.requests == []


def test_an_organizer_response_failure_reports_the_graph_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {
        ("POST", f"/me/events/{EVENT_ID}/accept"): httpx.Response(
            400,
            json={"error": {"code": "ErrorInvalidRequest", "message": "You can't respond to your own meeting."}},
        ),
    }
    tools, _graph = _tools(tmp_path, monkeypatch, routes)

    result = _payload(tools.outlook_calendar_respond_to_event(EVENT_ID, "accept"))

    assert (result["code"], result["status_code"], result["graph_code"]) == ("graph_error", 400, "ErrorInvalidRequest")
