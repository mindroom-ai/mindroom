"""Tests for Wave 8 Batch 2 native dev, scheduling, commerce & support toolkits."""

from __future__ import annotations

import json

import httpx
import pytest

from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tools.bitbucket import bitbucket_tools
from mindroom.tools.cal_com import cal_com_tools
from mindroom.tools.shopify import shopify_tools
from mindroom.tools.zendesk import zendesk_tools


def test_batch2_action_scoped_grants_and_no_env_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Batch 2 toolkits declare per-toolkit action_scoped_grants and never read host os.environ."""
    registry = builtin_tool_registry()
    assert registry.entries["bitbucket"].action_scoped_grants == ("bitbucket_read",)
    assert registry.entries["cal_com"].action_scoped_grants == ("cal_com_read", "cal_com_write")
    assert registry.entries["shopify"].action_scoped_grants == ("shopify_store", "shopify_analytics")
    assert registry.entries["zendesk"].action_scoped_grants == ()

    monkeypatch.setenv("BITBUCKET_USERNAME", "u")
    monkeypatch.setenv("BITBUCKET_TOKEN", "t")
    monkeypatch.setenv("CALCOM_API_KEY", "k")
    monkeypatch.setenv("CALCOM_EVENT_TYPE_ID", "42")
    monkeypatch.setenv("SHOPIFY_SHOP_NAME", "store")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ZENDESK_USERNAME", "u")
    monkeypatch.setenv("ZENDESK_PASSWORD", "p")
    monkeypatch.setenv("ZENDESK_COMPANY_NAME", "acme")

    with pytest.raises(ValueError, match="Bitbucket username"):
        bitbucket_tools()(workspace="w", repo_slug="r")
    assert "API key" in cal_com_tools()().cal_com_read(action="upcoming_bookings")
    assert "required" in json.loads(shopify_tools()().shopify_store(action="shop_info"))["error"]
    assert "required" in json.loads(zendesk_tools()().search_zendesk("help"))["error"]


def test_bitbucket_read_create_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bitbucket validates path segments, paginates safely on the same origin, and returns diffs."""
    with pytest.raises(ValueError, match="Invalid workspace"):
        bitbucket_tools()(username="u", token="t", workspace="../evil", repo_slug="repo")  # noqa: S106

    requests_seen: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        params: dict[str, object] | None = None,
        json: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> httpx.Response:
        requests_seen.append((method, url, params))
        req = httpx.Request(method, url)
        if url.endswith("/commits"):
            if params and params.get("page") == "2":
                return httpx.Response(200, request=req, json={"values": [{"hash": "c2"}]})
            return httpx.Response(
                200,
                request=req,
                json={"values": [{"hash": "c1"}], "next": "https://evil.example/steal?page=2"},
            )
        if url.endswith("/pullrequests/7/diff"):
            return httpx.Response(200, request=req, headers={"Content-Type": "text/plain"}, text="diff --git a b")
        if method == "POST" and url.endswith("/repositories/ws/repo"):
            return httpx.Response(200, request=req, json={"name": (json or {}).get("name")})
        return httpx.Response(200, request=req, json={"ok": True})

    monkeypatch.setattr(httpx, "request", fake_request)
    toolkit = bitbucket_tools()(username="u", token="t", workspace="ws", repo_slug="repo")  # noqa: S106

    commits = json.loads(toolkit.bitbucket_read(action="commits", count=2))
    assert [c["hash"] for c in commits["values"]] == ["c1", "c2"]
    assert all(url.startswith("https://api.bitbucket.org/2.0/") for _, url, _ in requests_seen)
    assert toolkit.bitbucket_read(action="pull_request_changes", pull_request_id=7) == "diff --git a b"
    created = json.loads(toolkit.bitbucket_create_repository(name="demo", project="ENG", is_private=True))
    assert created["name"] == "demo"


def test_cal_com_toggles_timezone_and_booking_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cal.com respects explicit None toggles, validates booking_uid, and formats timezones."""
    disabled = cal_com_tools()(
        api_key="cal_k",
        event_type_id=10,
        enable_get_available_slots=None,
        enable_get_upcoming_bookings=None,
        enable_create_booking=None,
        enable_reschedule_booking=None,
        enable_cancel_booking=None,
    )
    assert list(disabled.functions) == []

    def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        if url.endswith("/slots/available"):
            return httpx.Response(200, json={"data": {"slots": {"2026-12-01": [{"time": "2026-12-01T15:00:00Z"}]}}})
        return httpx.Response(
            200,
            json={"data": [{"uid": "b1", "title": "Intro", "start": "2026-12-01T15:00:00Z", "status": "ACCEPTED"}]},
        )

    def fake_post(url: str, **_kwargs: object) -> httpx.Response:
        if url.endswith("/bookings"):
            return httpx.Response(201, json={"data": {"uid": "b2", "start": "2026-12-01T15:00:00Z"}})
        if url.endswith("/cancel"):
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(201, json={"data": {"uid": "b3", "start": "2026-12-02T15:00:00Z"}})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    toolkit = cal_com_tools()(api_key="cal_k", event_type_id=10, user_timezone="UTC", enable_cancel_booking=False)
    assert "2026-12-01 15:00 UTC" in toolkit.cal_com_read(
        action="available_slots",
        start_date="2026-12-01",
        end_date="2026-12-02",
    )
    assert "b1" in toolkit.cal_com_read(action="upcoming_bookings")
    assert "b2" in toolkit.cal_com_write(
        action="create_booking",
        start_time="2026-12-01T15:00:00Z",
        name="Ada",
        email="ada@example.com",
    )
    assert (
        "disabled" in json.loads(toolkit.cal_com_write(action="cancel_booking", booking_uid="b1", reason="no"))["error"]
    )
    assert "Invalid booking_uid" in toolkit.cal_com_write(
        action="reschedule_booking",
        booking_uid="../escape",
        start_time="2026-12-02T15:00:00Z",
        reason="shift",
    )


def test_shopify_store_and_analytics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shopify validates shop_name, uses GraphQL variables, and computes analytics accurately."""
    with pytest.raises(ValueError, match="Invalid shop_name"):
        shopify_tools()(shop_name="evil.com/path", access_token="tok")  # noqa: S106

    sample_orders = [
        {
            "node": {
                "id": "gid://shopify/Order/1",
                "name": "#1001",
                "createdAt": "2026-11-01T10:00:00Z",
                "displayFinancialStatus": "PAID",
                "displayFulfillmentStatus": "FULFILLED",
                "totalPriceSet": {"shopMoney": {"amount": "50.00", "currencyCode": "USD"}},
                "subtotalPriceSet": {"shopMoney": {"amount": "45.00"}},
                "totalShippingPriceSet": {"shopMoney": {"amount": "5.00"}},
                "totalTaxSet": {"shopMoney": {"amount": "0.00"}},
                "customer": {
                    "id": "gid://shopify/Customer/1",
                    "email": "ada@example.com",
                    "firstName": "Ada",
                    "lastName": "Lovelace",
                    "numberOfOrders": 2,
                    "amountSpent": {"amount": "100.00", "currencyCode": "USD"},
                },
                "lineItems": {
                    "edges": [
                        {
                            "node": {
                                "id": "li1",
                                "title": "Widget",
                                "quantity": 2,
                                "variant": {
                                    "id": "v1",
                                    "title": "Default",
                                    "sku": "W1",
                                    "product": {"id": "gid://shopify/Product/1", "title": "Widget"},
                                },
                                "originalUnitPriceSet": {"shopMoney": {"amount": "20.00", "currencyCode": "USD"}},
                            },
                        },
                        {
                            "node": {
                                "id": "li2",
                                "title": "Gadget",
                                "quantity": 1,
                                "variant": {
                                    "id": "v2",
                                    "title": "Default",
                                    "sku": "G1",
                                    "product": {"id": "gid://shopify/Product/2", "title": "Gadget"},
                                },
                                "originalUnitPriceSet": {"shopMoney": {"amount": "5.00", "currencyCode": "USD"}},
                            },
                        },
                    ],
                },
            },
        },
    ]

    def fake_post(url: str, *, json: dict[str, object], **_kwargs: object) -> httpx.Response:
        assert url == "https://acme.myshopify.com/admin/api/2025-10/graphql.json"
        req = httpx.Request("POST", url)
        query = str(json["query"])
        assert "{{" not in query
        if "shop {" in query:
            return httpx.Response(200, request=req, json={"data": {"shop": {"name": "Acme"}}})
        if "products(" in query:
            return httpx.Response(
                200,
                request=req,
                json={
                    "data": {
                        "products": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "gid://shopify/Product/1",
                                        "title": "Widget",
                                        "status": "ACTIVE",
                                        "totalInventory": 3,
                                        "variants": {
                                            "edges": [
                                                {
                                                    "node": {
                                                        "id": "v1",
                                                        "title": "S",
                                                        "sku": "W1",
                                                        "inventoryQuantity": 3,
                                                    },
                                                },
                                            ],
                                        },
                                    },
                                },
                            ],
                        },
                    },
                },
            )
        return httpx.Response(200, request=req, json={"data": {"orders": {"edges": sample_orders}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    toolkit = shopify_tools()(shop_name="acme.myshopify.com", access_token="shpat_123")  # noqa: S106

    assert json.loads(toolkit.shopify_store(action="shop_info"))["name"] == "Acme"
    low = json.loads(toolkit.shopify_store(action="low_stock", threshold=5))
    assert len(low) == 1
    assert low[0]["title"] == "Widget"
    top = json.loads(toolkit.shopify_analytics(action="top_selling"))
    assert top[0]["title"] == "Widget"
    assert top[0]["total_quantity"] == 2
    pairs = json.loads(toolkit.shopify_analytics(action="bought_together", min_occurrences=1))
    assert pairs[0]["times_bought_together"] == 1
    aov = json.loads(toolkit.shopify_analytics(action="average_order_value"))
    assert aov["overall_average_order_value"] == 50.0


def test_zendesk_validation_toggles_and_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zendesk validates subdomain labels, respects explicit None toggles, and strips HTML."""
    with pytest.raises(ValueError, match="Invalid company_name"):
        zendesk_tools()(username="u", password="p", company_name="evil.com#")  # noqa: S106

    disabled = zendesk_tools()(username="u", password="p", company_name="acme", enable_search_zendesk=None)  # noqa: S106
    assert list(disabled.functions) == []

    def fake_get(url: str, *, params: dict[str, str], auth: tuple[str, str], **_kwargs: object) -> httpx.Response:
        assert url == "https://acme.zendesk.com/api/v2/help_center/articles/search.json"
        assert params == {"query": "reset password"}
        assert auth == ("u", "p")
        req = httpx.Request("GET", url)
        return httpx.Response(200, request=req, json={"results": [{"body": "<p>Reset <b>here</b></p>"}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    toolkit = zendesk_tools()(username="u", password="p", company_name="acme")  # noqa: S106
    assert json.loads(toolkit.search_zendesk("reset password")) == ["Reset here"]


def test_bitbucket_pr_diff_follows_same_origin_redirect_and_rejects_cross_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bitbucket PR diff follows same-origin 302 redirects and rejects cross-origin redirects without leaking auth."""
    requests_seen: list[tuple[str, str, dict[str, str]]] = []
    redirect_targets = {
        7: "https://api.bitbucket.org/2.0/repositories/ws/repo/diff/ws/repo:abc..def?topic=true",
        8: "/2.0/repositories/ws/repo/diff/ws/repo:rel",
        9: "https://evil.example/steal-auth",
        10: "http://api.bitbucket.org/2.0/repositories/ws/repo/diff/downgrade",
        11: "https://api.bitbucket.org:8443/2.0/repositories/ws/repo/diff/other-port",
    }

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool = False,
        **_kwargs: object,
    ) -> httpx.Response:
        assert follow_redirects is False
        requests_seen.append((method, url, headers))
        req = httpx.Request(method, url)
        for pr_id, target in redirect_targets.items():
            if url.endswith(f"/pullrequests/{pr_id}/diff"):
                return httpx.Response(302, request=req, headers={"Location": target})
        if "/diff/ws/repo:abc..def" in url:
            return httpx.Response(
                200,
                request=req,
                headers={"Content-Type": "text/plain; charset=utf-8"},
                text="diff A",
            )
        if url.endswith("/diff/ws/repo:rel"):
            return httpx.Response(200, request=req, headers={"Content-Type": "text/plain"}, text="diff B")
        pytest.fail(f"Unexpected request sent to {url}")

    monkeypatch.setattr(httpx, "request", fake_request)
    toolkit = bitbucket_tools()(username="u", token="secret-tok", workspace="ws", repo_slug="repo")  # noqa: S106

    assert toolkit.bitbucket_read(action="pull_request_changes", pull_request_id=7) == "diff A"
    assert toolkit.bitbucket_read(action="pull_request_changes", pull_request_id=8) == "diff B"

    for bad_pr in (9, 10, 11):
        err = json.loads(toolkit.bitbucket_read(action="pull_request_changes", pull_request_id=bad_pr))
        assert "cross-origin redirect" in err["error"]

    assert all(url.startswith("https://api.bitbucket.org/") for _, url, _ in requests_seen)


def test_shopify_analytics_uses_minimal_report_specific_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aggregate Shopify reports omit customer and unneeded lineItems fields to avoid PCD permission errors."""
    queries_by_call: list[str] = []

    def fake_post(url: str, *, json: dict[str, object], **_kwargs: object) -> httpx.Response:
        req = httpx.Request("POST", url)
        query = str(json["query"])
        queries_by_call.append(query)
        order_node = {
            "id": "gid://shopify/Order/1",
            "name": "#1001",
            "createdAt": "2026-11-01T10:00:00Z",
            "displayFinancialStatus": "PAID",
            "displayFulfillmentStatus": "FULFILLED",
            "totalPriceSet": {"shopMoney": {"amount": "50.00", "currencyCode": "USD"}},
            "subtotalPriceSet": {"shopMoney": {"amount": "45.00"}},
            "totalShippingPriceSet": {"shopMoney": {"amount": "5.00"}},
            "totalTaxSet": {"shopMoney": {"amount": "0.00"}},
            "customer": {
                "id": "gid://shopify/Customer/1",
                "email": "ada@example.com",
                "firstName": "Ada",
                "lastName": "Lovelace",
                "numberOfOrders": 2,
                "amountSpent": {"amount": "100.00", "currencyCode": "USD"},
            },
            "lineItems": {
                "edges": [
                    {
                        "node": {
                            "title": "Widget",
                            "quantity": 2,
                            "variant": {
                                "id": "v1",
                                "title": "Default",
                                "sku": "W1",
                                "product": {"id": "gid://shopify/Product/1", "title": "Widget"},
                            },
                            "originalUnitPriceSet": {"shopMoney": {"amount": "20.00", "currencyCode": "USD"}},
                        },
                    },
                ],
            },
        }
        return httpx.Response(200, request=req, json={"data": {"orders": {"edges": [{"node": order_node}]}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    toolkit = shopify_tools()(shop_name="acme", access_token="shpat_123")  # noqa: S106

    aov = json.loads(toolkit.shopify_analytics(action="average_order_value"))
    assert aov["overall_average_order_value"] == 50.0
    aov_query = queries_by_call[-1]
    assert "createdAt" in aov_query
    assert "totalPriceSet" in aov_query
    assert "customer" not in aov_query
    assert "lineItems" not in aov_query

    toolkit.shopify_analytics(action="sales_trends", compare_previous_period=False)
    assert "customer" not in queries_by_call[-1]

    toolkit.shopify_analytics(action="order_analytics")
    assert "customer" not in queries_by_call[-1]

    toolkit.shopify_analytics(action="top_selling")
    assert "customer" not in queries_by_call[-1]

    toolkit.shopify_analytics(action="repeat_customers", min_occurrences=1)
    repeat_query = queries_by_call[-1]
    assert "customer" in repeat_query
    assert "email" in repeat_query
    assert "lineItems" not in repeat_query

    toolkit.shopify_analytics(action="customer_orders", customer_email="ada@example.com")
    customer_orders_query = queries_by_call[-1]
    assert "customer" in customer_orders_query
    assert "email" not in customer_orders_query


def test_shopify_repeat_customers_reports_total_count_before_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """repeat_customers reports repeat_customer_count before applying limit truncation."""

    def make_order(cid: str, email: str, first: str, amount: str) -> dict[str, object]:
        return {
            "node": {
                "totalPriceSet": {"shopMoney": {"amount": amount}},
                "customer": {
                    "id": cid,
                    "email": email,
                    "firstName": first,
                    "lastName": "Example",
                    "numberOfOrders": 5,
                    "amountSpent": {"amount": "200.00", "currencyCode": "USD"},
                },
            },
        }

    orders = [
        make_order("c1", "ada@example.com", "Ada", "50.00"),
        make_order("c1", "ada@example.com", "Ada", "40.00"),
        make_order("c1", "ada@example.com", "Ada", "30.00"),
        make_order("c2", "grace@example.com", "Grace", "25.00"),
        make_order("c2", "grace@example.com", "Grace", "25.00"),
        make_order("c3", "bob@example.com", "Bob", "10.00"),
    ]

    def fake_post(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {"orders": {"edges": orders}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    toolkit = shopify_tools()(shop_name="acme", access_token="shpat_123")  # noqa: S106

    result = json.loads(
        toolkit.shopify_analytics(
            action="repeat_customers",
            min_occurrences=2,
            limit=1,
            created_after="2026-11-01",
            created_before="2026-11-30",
        ),
    )
    assert result["repeat_customer_count"] == 2
    assert result["min_orders_threshold"] == 2
    assert result["period"] == {"created_after": "2026-11-01", "created_before": "2026-11-30"}
    assert len(result["customers"]) == 1
    assert result["customers"][0]["email"] == "ada@example.com"
    assert result["customers"][0]["orders_in_period"] == 3
