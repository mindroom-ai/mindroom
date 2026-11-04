"""The Connections portal's contacts routes: what an owner sees of their contacts, and what they change there."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from mindroom.api import config_lifecycle, main
from mindroom.config.main import Config
from mindroom.config.personal_agents import PersonalAgentRoster, personal_agent_roster_path
from mindroom.contacts import flow
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, read_contacts, update_contacts
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.personal_room_store import PersonalRoomRecord, personal_room_record_path, write_personal_room
from tests.api.conftest import (
    publish_api_payload,
    trusted_upstream_env,
    trusted_upstream_jwks,
    trusted_upstream_jwt,
    trusted_upstream_jwt_key,
)
from tests.api.test_oauth_api import _runtime_paths
from tests.conftest import make_conversation_reader_mock, make_latest_thread_event_id_mock, make_matrix_client_mock
from tests.runtime_fakes import bind_test_embedded_runtime, ready_runtime
from tests.test_generated_agents import _personal

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
CAROL = "@carol:example.org"
ORIGIN = "https://portal.example.org"
BOB_ROOM = "!bobroom:example.org"
CONTACT_ROOM = "!contact:example.org"
PAYLOAD: dict[str, Any] = {
    "models": {"default": {"provider": "ollama", "id": "test-model"}},
    "rooms": {"lobby": {}},
    "personal_agents": {
        "mind": {
            "agent": {
                "display_name": "{user_display_name}'s Mind",
                "model": "default",
                "tools": ["calculator", "shell", "file", "duckdb", "google_drive", "scheduler"],
                "include_default_tools": False,
                "contacts": {"rules": ["Never share keys."], "tools": ["calculator"]},
            },
        },
    },
    "personal_rooms": {"agent": "mind", "onboarding_rooms": ["lobby"]},
    "authorization": {"aliases": {BOB: ["@bobalias:example.org"]}},
}


def _bots() -> dict[str, MagicMock]:
    """Running bots for every personal agent, whose sends and room changes succeed."""
    bots = {}
    for name in ("mind_alice", "mind_bob", "mind_carol"):
        bots[name] = bot = MagicMock(running=True, client=make_matrix_client_mock(user_id=f"@{name}:example.org"))
        bot.hook_context.send_message = AsyncMock(return_value="$kickoff")
        bot.conversation_reader = make_conversation_reader_mock()
        bot.conversation_reader.latest_thread_event_id = make_latest_thread_event_id_mock()
        bot.change_local_membership = AsyncMock(return_value=True)
        bot.current_joined_room_ids = AsyncMock(return_value=frozenset())
    return bots


@pytest.fixture
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforce_turn_authorization: None) -> dict[str, Any]:  # noqa: ARG001
    """Serve the real API to signed owners of three personal agents with contacts, and Bob's personal room."""
    key = trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: trusted_upstream_jwks(key))
    env = {**trusted_upstream_env(), "MINDROOM_PUBLIC_URL": ORIGIN, "MATRIX_HOMESERVER": "http://localhost:8008"}
    paths = _runtime_paths(tmp_path, env)
    roster = personal_agent_roster_path(paths)
    roster.parent.mkdir(parents=True, exist_ok=True)
    roster.write_text(
        PersonalAgentRoster(entries=[_personal(ALICE), _personal(BOB), _personal(CAROL)]).model_dump_json(),
        encoding="utf-8",
    )
    record = PersonalRoomRecord(user_id=BOB, alias="personal-mind_bob", source_room_id="!lobby:example.org")
    write_personal_room(
        personal_room_record_path(paths, "mind_bob", BOB),
        record.model_copy(update={"room_id": BOB_ROOM}),
    )
    bots = _bots()
    main.initialize_api_app(main.app, paths)
    bind_test_embedded_runtime(main.app, agent_bot=bots.get)
    publish_api_payload(main.app, PAYLOAD)
    headers = {
        name: {
            "X-Trusted-Jwt": trusted_upstream_jwt(
                key,
                user_id=name,
                email=f"{name}@example.org",
                matrix_user_id=f"@{name}:example.org",
            ),
            "Origin": ORIGIN,
        }
        for name in ("alice", "bob", "bobalias", "carol", "mallory")
    }
    return {"client": TestClient(main.app, base_url=ORIGIN), "headers": headers, "paths": paths, "bots": bots}


def _agent(name: str, owner: str) -> ContactParty:
    return ContactParty(kind="agent", id=name, owner=owner)


def _seed(paths: RuntimePaths, *records: ContactRecord) -> None:
    update_contacts(paths, lambda book: book.contacts.extend(records))


def _contact(contact_id: str, requester: ContactParty, target: ContactParty, **fields: Any) -> ContactRecord:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": contact_id,
        "status": "pending",
        "requester": requester,
        "target": target,
        "reason": "Plan the dinner",
        "requester_side": ContactSide(rules="Alice's secret rule"),
        "target_side": ContactSide(rules="Bob's secret rule", tools=["calculator", "file"]),
        "origin_room_id": "!aliceroom:example.org",
        "origin_thread_id": "$ask",
        "notice_event_id": "$notice",
        "requested_at": datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
    }
    return ContactRecord.model_validate(values | fields)


def _get(portal: dict[str, Any], user: str) -> dict[str, Any]:
    response = portal["client"].get("/api/connections/contacts", headers=portal["headers"][user])
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    return response.json()


def _post(portal: dict[str, Any], user: str, path: str, body: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
    return portal["client"].post(
        f"/api/connections/contacts/{path}",
        headers=portal["headers"][user],
        json={} if body is None else body,
    )


def test_listing_shows_only_the_viewers_contacts_and_their_side(portal: dict[str, Any]) -> None:
    """Alice sees her contact with her side's terms only, and not a contact between two other owners."""
    _seed(
        portal["paths"],
        _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB)),
        _contact("bc", _agent("mind_bob", BOB), _agent("mind_carol", CAROL)),
    )
    page = _get(portal, "alice")
    assert [contact["id"] for contact in page["contacts"]] == ["ab"]
    (contact,) = page["contacts"]
    assert contact["side"] == "requester"
    assert contact["you"] == {"kind": "agent", "id": "mind_alice", "owner": ALICE, "display_name": "Alice's Mind"}
    assert contact["other"] == {"kind": "agent", "id": "mind_bob", "owner": BOB, "display_name": "Bob's Mind"}
    assert contact["rules"] == "Alice's secret rule"
    assert contact["effective_tools"] == ["calculator", "matrix_message"]
    assert contact["room_id"] is None
    assert contact["requested_at"] == "2026-11-03T09:00:00Z"
    assert "Bob's secret rule" not in str(page)
    (agent,) = page["agents"]
    assert agent["agent_name"] == "mind_alice"
    assert (agent["configured_rules"], agent["judged"]) == (["Never share keys."], False)


def test_a_bridge_alias_sees_its_canonical_owners_contacts(portal: dict[str, Any]) -> None:
    """Signed in through a bridge alias, Bob sees his contacts and the Contacts link, and acts on them as himself."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB), status="active"))
    assert [contact["id"] for contact in _get(portal, "bobalias")["contacts"]] == ["ab"]
    me = portal["client"].get("/api/connections/me", headers=portal["headers"]["bobalias"])
    assert me.json()["contacts_enabled"] is True
    response = _post(portal, "bobalias", "ab/update", {"rules": "Only dinner."})
    assert response.status_code == 200, response.text
    assert read_contacts(portal["paths"]).contacts[0].target_side.rules == "Only dinner."


def test_listing_leaves_out_removed_contacts_and_shows_only_an_active_room(portal: dict[str, Any]) -> None:
    """A removed contact is not listed, and only an active contact names its room, not one still being set up."""
    _seed(
        portal["paths"],
        _contact("gone", _agent("mind_alice", ALICE), _agent("mind_bob", BOB), status="removed", room_id="!old:x"),
        _contact(
            "live",
            _agent("mind_alice", ALICE),
            _agent("mind_carol", CAROL),
            status="active",
            room_id=CONTACT_ROOM,
        ),
        _contact("mid", _agent("mind_alice", ALICE), _agent("mind_bob", BOB), room_id="!setup:example.org"),
    )
    contacts = _get(portal, "alice")["contacts"]
    assert [(contact["id"], contact["room_id"]) for contact in contacts] == [("live", CONTACT_ROOM), ("mid", None)]


def test_listing_says_what_each_tool_a_contact_may_use_reaches(portal: dict[str, Any]) -> None:
    """Each choice says whether it runs code, is not confined to the agent's files, reaches them, or neither."""
    choices = {choice["name"]: choice for choice in _get(portal, "alice")["agents"][0]["tool_choices"]}
    assert set(choices) == {"calculator", "shell", "file", "duckdb", "google_drive"}
    assert {name: choice["reach"] for name, choice in choices.items()} == {
        "calculator": "none",
        "shell": "code",
        "file": "files",
        "duckdb": "unconfined",
        "google_drive": "files",
    }
    assert choices["google_drive"]["provider"] == "google_drive"
    assert choices["calculator"] == {
        "name": "calculator",
        "display_name": "Calculator",
        "provider": None,
        "reach": "none",
    }


def test_tools_of_an_agent_with_unrestricted_file_access_are_not_confined(portal: dict[str, Any]) -> None:
    """With `file_access: unrestricted`, a tool that would reach only the agent's files reaches beyond them."""
    payload = {**PAYLOAD, "personal_agents": {"mind": {"agent": {**PAYLOAD["personal_agents"]["mind"]["agent"]}}}}
    payload["personal_agents"]["mind"]["agent"]["file_access"] = "unrestricted"
    publish_api_payload(main.app, payload)
    choices = {choice["name"]: choice["reach"] for choice in _get(portal, "alice")["agents"][0]["tool_choices"]}
    assert (choices["file"], choices["shell"], choices["calculator"]) == ("unconfined", "code", "none")


def test_a_tool_served_by_a_shared_service_account_names_no_account_of_the_owner(
    portal: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Google tool that a configured service account serves does not act with the owner's account."""
    monkeypatch.setattr(
        "mindroom.api.connections_contacts.oauth_provider_service_account_configured",
        lambda provider, _paths: provider.id == "google_drive",
    )
    choices = {choice["name"]: choice for choice in _get(portal, "alice")["agents"][0]["tool_choices"]}
    assert choices["google_drive"]["provider"] is None


def test_a_side_whose_agent_has_no_contacts_shows_no_terms(portal: dict[str, Any]) -> None:
    """A contact whose agent left the config still lists, without terms to show or change."""
    _seed(portal["paths"], _contact("ab", _agent("mind_gone", ALICE), _agent("mind_bob", BOB)))
    (contact,) = _get(portal, "alice")["contacts"]
    assert (contact["rules"], contact["effective_tools"]) == (None, None)
    assert contact["you"]["display_name"] == "mind_gone"


def test_update_changes_only_the_viewers_side(portal: dict[str, Any]) -> None:
    """An owner's update sets their own side's rules and tools and answers with the new listing."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB), status="active"))
    response = _post(portal, "bob", "ab/update", {"rules": "Only dinner.", "tools": ["calculator", "shell"]})
    assert response.status_code == 200, response.text
    (contact,) = response.json()["contacts"]
    assert (contact["rules"], contact["effective_tools"]) == ("Only dinner.", ["calculator", "shell", "matrix_message"])
    (stored,) = read_contacts(portal["paths"]).contacts
    assert stored.target_side == ContactSide(rules="Only dinner.", tools=["calculator", "shell"])
    assert stored.requester_side == ContactSide(rules="Alice's secret rule")


def test_update_refuses_tools_a_contact_may_not_use(portal: dict[str, Any]) -> None:
    """A tool the agent lacks or a contact never offers is refused with the reason, and nothing changes."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB), status="active"))
    response = _post(portal, "bob", "ab/update", {"tools": ["scheduler"]})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("Not among the tools a contact may use: scheduler")
    assert read_contacts(portal["paths"]).contacts[0].target_side.tools == ["calculator", "file"]


@pytest.mark.parametrize("action", ["accept", "decline", "update", "remove"])
def test_changes_require_the_portals_own_origin(portal: dict[str, Any], action: str) -> None:
    """A change from another origin, or with no origin, is refused before anything is read or written."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB)))
    headers = {key: value for key, value in portal["headers"]["bob"].items() if key != "Origin"}
    body = {"rules": "x"} if action == "update" else {}
    for extra in ({}, {"Origin": "https://evil.example.org"}):
        response = portal["client"].post(f"/api/connections/contacts/ab/{action}", headers=headers | extra, json=body)
        assert response.status_code == 403, response.text
    assert read_contacts(portal["paths"]).contacts[0].status == "pending"


def test_another_owners_contact_is_not_found(portal: dict[str, Any]) -> None:
    """A contact the viewer owns no side of reads as no contact at all."""
    _seed(portal["paths"], _contact("bc", _agent("mind_bob", BOB), _agent("mind_carol", CAROL)))
    response = _post(portal, "alice", "bc/remove")
    assert (response.status_code, response.json()["detail"]) == (409, "There is no contact bc")
    assert read_contacts(portal["paths"]).contacts[0].status == "pending"


@pytest.mark.parametrize("action", ["accept", "decline", "remove"])
def test_changes_that_move_agents_are_refused_while_agents_restart(portal: dict[str, Any], action: str) -> None:
    """While a reload swaps the agents, accepting, declining, and removing are refused and nothing changes."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB)))
    gate = config_lifecycle.app_state(main.app).runtime.response_admission_gate
    gate.close()
    try:
        response = _post(portal, "bob", f"ab/{action}")
    finally:
        gate.reopen()
    assert response.status_code == 503
    assert response.json()["detail"] == "MindRoom is restarting its agents; try again in a moment."
    assert read_contacts(portal["paths"]).contacts[0].status == "pending"


def test_updating_needs_no_agents_so_it_works_while_they_restart(portal: dict[str, Any]) -> None:
    """Changing rules only writes the store, so a reload in progress does not refuse it."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB), status="active"))
    gate = config_lifecycle.app_state(main.app).runtime.response_admission_gate
    gate.close()
    try:
        response = _post(portal, "bob", "ab/update", {"rules": "Only dinner."})
    finally:
        gate.reopen()
    assert response.status_code == 200, response.text


def test_changes_give_back_their_response_slot_whether_they_succeed_or_are_refused(portal: dict[str, Any]) -> None:
    """A change that holds a slot frees it after succeeding and after a refusal, so a reload is never kept waiting."""
    _seed(
        portal["paths"],
        _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB)),
        _contact("bc", _agent("mind_bob", BOB), _agent("mind_carol", CAROL)),
    )
    gate = config_lifecycle.app_state(main.app).runtime.response_admission_gate
    with patch("mindroom.contacts.flow.send_message_result", new=AsyncMock(return_value=None)):
        assert _post(portal, "bob", "ab/decline").status_code == 200
    assert gate.in_flight_response_count == 0
    assert _post(portal, "alice", "bc/remove").status_code == 409
    assert gate.in_flight_response_count == 0
    assert _post(portal, "bob", "bc/remove").status_code == 200
    assert gate.in_flight_response_count == 0


def test_accepting_for_an_agent_gone_from_the_config_names_no_other_agent(portal: dict[str, Any]) -> None:
    """Accepting with tools for a side whose agent left the config is refused without listing the configured agents."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_gone", BOB)))
    response = _post(portal, "bob", "ab/accept", {"tools": []})
    assert response.status_code == 409
    assert response.json()["detail"] == "contacts are not enabled for mind_gone, so the contact room cannot be set up"


def test_accepting_acts_as_the_signed_in_owner_with_the_runtimes_bots(portal: dict[str, Any]) -> None:
    """The acceptance runs for the signed-in owner with no agent of theirs, and the terms they chose."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB)))
    with patch.object(flow, "accept_contact", new=AsyncMock()) as accept:
        response = _post(portal, "bob", "ab/accept", {"rules": "Be brief.", "tools": ["calculator"]})
    assert response.status_code == 200, response.text
    (actor, contact_id), terms = accept.await_args.args, accept.await_args.kwargs
    assert (actor.owner, actor.agent_name, contact_id, terms) == (
        BOB,
        None,
        "ab",
        {"rules": "Be brief.", "tools": ["calculator"]},
    )
    assert actor.agent_bot("mind_bob") is portal["bots"]["mind_bob"]


def test_declining_declines_and_answers_the_request_thread(portal: dict[str, Any]) -> None:
    """A decline on the page stores the decision and Bob's personal-room agent says so in the request's thread."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB)))
    delivered = DeliveredMatrixEvent("$posted", {})
    with patch("mindroom.contacts.flow.send_message_result", new=AsyncMock(return_value=delivered)) as send:
        response = _post(portal, "bob", "ab/decline")
    assert response.status_code == 200, response.text
    assert response.json()["contacts"][0]["status"] == "declined"
    posts = {(call.args[1], call.args[2]["body"]) for call in send.await_args_list}
    assert (BOB_ROOM, "Declined on the Connections page.") in posts


def test_removing_makes_the_agents_leave_and_drops_the_contact(portal: dict[str, Any]) -> None:
    """Removing an active contact makes both agents leave its room, and the listing no longer shows it."""
    _seed(
        portal["paths"],
        _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB), status="active", room_id=CONTACT_ROOM),
    )
    response = _post(portal, "alice", "ab/remove")
    assert response.status_code == 200, response.text
    assert response.json()["contacts"] == []
    for name in ("mind_alice", "mind_bob"):
        portal["bots"][name].change_local_membership.assert_awaited_once_with(CONTACT_ROOM, "leave")


def test_a_corrupt_store_is_unavailable_without_details(portal: dict[str, Any]) -> None:
    """An unreadable contacts file answers 503 without naming the file."""
    path = portal["paths"].storage_root / "tracking" / "contacts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    for response in (
        portal["client"].get("/api/connections/contacts", headers=portal["headers"]["alice"]),
        _post(portal, "alice", "ab/remove"),
    ):
        assert (response.status_code, response.json()["detail"]) == (503, "Contacts are unavailable")


def test_query_parameters_and_unsigned_requests_are_refused(portal: dict[str, Any]) -> None:
    """The routes take no target from the address and need a signed identity."""
    client = portal["client"]
    assert (
        client.get("/api/connections/contacts?owner=@bob:example.org", headers=portal["headers"]["alice"]).status_code
        == 400
    )
    assert client.get("/api/connections/contacts").status_code == 401


def test_bodies_with_unknown_fields_are_refused(portal: dict[str, Any]) -> None:
    """A change takes only the fields it names, so a misspelled field is refused rather than ignored."""
    _seed(portal["paths"], _contact("ab", _agent("mind_alice", ALICE), _agent("mind_bob", BOB)))
    assert _post(portal, "bob", "ab/decline", {"reason": "no"}).status_code == 422
    assert _post(portal, "bob", "ab/accept", {"tool": ["calculator"]}).status_code == 422
    assert read_contacts(portal["paths"]).contacts[0].status == "pending"


def test_me_says_contacts_are_on_for_an_owner_of_an_agent_with_contacts(portal: dict[str, Any]) -> None:
    """An owner of a personal agent with contacts gets the Contacts page link, and someone without one does not."""
    for user, enabled in (("alice", True), ("mallory", False)):
        response = portal["client"].get("/api/connections/me", headers=portal["headers"][user])
        assert response.status_code == 200, response.text
        assert response.json()["contacts_enabled"] is enabled


def test_the_runtime_lends_the_api_its_running_agent_bots(tmp_path: Path) -> None:
    """The portal reaches the bots the runtime runs now, including one started after the API was served."""
    runtime = ready_runtime(Config(), _runtime_paths(tmp_path, {}))
    lent = runtime.embedded_runtime()
    bot = MagicMock()
    runtime.fleet.add("mind_bob", bot)
    assert (lent.agent_bot("mind_bob"), lent.agent_bot("mind_gone")) == (bot, None)
