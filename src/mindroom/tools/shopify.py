"""Shopify Admin GraphQL store & analytics toolkit."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any, Literal

import httpx

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    error_json,
    json_dumps,
    require_non_empty,
    safe_host_label,
    safe_path_segment,
)

_StoreAction = Literal["shop_info", "products", "orders", "inventory_levels", "low_stock"]
_AnalyticsAction = Literal[
    "top_selling",
    "bought_together",
    "sales_by_date",
    "order_analytics",
    "product_breakdown",
    "customer_orders",
    "sales_trends",
    "average_order_value",
    "repeat_customers",
]
_GroupBy = Literal["day", "week", "month"]

_QUERY_SHOP_INFO = (
    "query { shop { name email currencyCode primaryDomain { url }"
    " billingAddress { country city } plan { displayName } } }"
)
_QUERY_PRODUCTS = (
    "query Products($first: Int!, $query: String) { products(first: $first, query: $query) { edges { node {"
    " id title status totalInventory tracksInventory createdAt updatedAt"
    " priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount currencyCode } }"
    " variants(first: 50) { edges { node { id title sku price inventoryQuantity inventoryPolicy } } }"
    " } } } }"
)
_ORDER_META = "id name createdAt displayFinancialStatus displayFulfillmentStatus"
_TOTAL_MONEY = "totalPriceSet { shopMoney { amount currencyCode } }"
_QTY_ITEMS = "lineItems(first: 100) { edges { node { quantity } } }"
_CUSTOMER_SUMMARY = "customer { id firstName lastName numberOfOrders amountSpent { amount currencyCode } }"
_CUSTOMER_DETAIL = "customer { id email firstName lastName numberOfOrders amountSpent { amount currencyCode } }"
_ORDER_SELECTIONS: dict[str, str] = {
    "average_order_value": f"createdAt {_TOTAL_MONEY}",
    "sales_trends": f"{_TOTAL_MONEY} {_QTY_ITEMS}",
    "sales_by_date": f"createdAt {_TOTAL_MONEY} {_QTY_ITEMS}",
    "order_analytics": (
        f"displayFinancialStatus displayFulfillmentStatus {_TOTAL_MONEY}"
        " subtotalPriceSet { shopMoney { amount } } totalShippingPriceSet { shopMoney { amount } }"
        f" totalTaxSet {{ shopMoney {{ amount }} }} {_QTY_ITEMS}"
    ),
    "top_selling": (
        "lineItems(first: 100) { edges { node {"
        " quantity variant { product { id title } } originalUnitPriceSet { shopMoney { amount } } } } }"
    ),
    "bought_together": "lineItems(first: 100) { edges { node { variant { product { id title } } } } }",
    "product_breakdown": (
        "createdAt lineItems(first: 100) { edges { node { quantity"
        " variant { id title sku product { id title } }"
        " originalUnitPriceSet { shopMoney { amount currencyCode } } } } }"
    ),
    "repeat_customers": f"totalPriceSet {{ shopMoney {{ amount }} }} {_CUSTOMER_DETAIL}",
    "customer_orders": (
        f"{_ORDER_META} {_TOTAL_MONEY} {_CUSTOMER_SUMMARY} lineItems(first: 100) {{ edges {{ node {{ title quantity }} }} }}"
    ),
    "orders": (
        f"{_ORDER_META} {_TOTAL_MONEY} subtotalPriceSet {{ shopMoney {{ amount }} }}"
        " customer { id email firstName lastName } lineItems(first: 100) { edges { node {"
        " id title quantity variant { sku } originalUnitPriceSet { shopMoney { amount } } } } }"
    ),
}


def _orders_query(selection: str) -> str:
    fields = _ORDER_SELECTIONS[selection]
    return (
        "query Orders($first: Int!, $query: String, $reverse: Boolean!) {"
        " orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: $reverse) {"
        f" edges {{ node {{ {fields} }} }} }} }}"
    )


def _normalize_shop_name(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().lower().removesuffix(".myshopify.com")
    return safe_host_label(cleaned, "shop_name")


def _build_order_query(
    *,
    paid_only: bool = False,
    status: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    customer_email: str | None = None,
    before_exclusive: bool = False,
) -> str | None:
    parts: list[str] = []
    if paid_only:
        parts.append("financial_status:paid")
    elif status:
        parts.append(f"financial_status:{status}")
    if created_after:
        parts.append(f"created_at:>={created_after}")
    if created_before:
        parts.append(f"created_at:{'<' if before_exclusive else '<='}{created_before}")
    if customer_email:
        parts.append(f"email:{customer_email}")
    return " AND ".join(parts) if parts else None


def _shop_amount(node: dict[str, Any], key: str) -> float:
    return float(node.get(key, {}).get("shopMoney", {}).get("amount", 0) or 0)


def _shop_currency(node: dict[str, Any], key: str = "totalPriceSet") -> str | None:
    return node.get(key, {}).get("shopMoney", {}).get("currencyCode")


def _edges_nodes(container: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [edge["node"] for edge in container.get(key, {}).get("edges", []) if isinstance(edge, dict)]


def _customer_name(customer: dict[str, Any]) -> str:
    return f"{customer.get('firstName', '')} {customer.get('lastName', '')}".strip()


def _pct_change(current: float, previous: float) -> float:
    if previous > 0:
        return ((current - previous) / previous) * 100
    return 100.0 if current > 0 else 0.0


def _trend_label(change: float) -> str:
    return "up" if change > 0 else ("down" if change < 0 else "flat")


class _ShopifyTools(Toolkit):
    """Read store data, inventory, orders, and sales analytics via the Shopify Admin GraphQL API."""

    def __init__(
        self,
        shop_name: str | None = None,
        access_token: str | None = None,
        api_version: str = "2025-10",
        timeout: int = 30,
    ) -> None:
        self.shop_name = _normalize_shop_name(shop_name)
        self.access_token = access_token.strip() if isinstance(access_token, str) and access_token.strip() else None
        self.api_version = safe_path_segment(require_non_empty(api_version, "api_version"), "api_version")
        self.timeout = timeout
        self.base_url = (
            f"https://{self.shop_name}.myshopify.com/admin/api/{self.api_version}/graphql.json"
            if self.shop_name
            else None
        )
        super().__init__(name="shopify", tools=[self.shopify_store, self.shopify_analytics])

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.base_url or not self.access_token:
            return {"error": "Shopify shop_name and access_token are required."}
        headers = {"X-Shopify-Access-Token": self.access_token, "Content-Type": "application/json"}
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        try:
            response = httpx.post(self.base_url, headers=headers, json=body, timeout=self.timeout)
            response.raise_for_status()
            result = response.json()
            if "errors" in result:
                return {"error": result["errors"]}
            data = result.get("data")
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError) as exc:
            return {"error": str(exc)}

    def _fetch_orders(
        self,
        selection: str,
        *,
        limit: int = 250,
        query_filter: str | None = None,
        reverse: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        variables: dict[str, Any] = {"first": max(1, min(limit, 250)), "reverse": reverse}
        if query_filter:
            variables["query"] = query_filter
        result = self._graphql(_orders_query(selection), variables)
        return result if "error" in result else _edges_nodes(result, "orders")

    def _fetch_products(self, *, limit: int, status: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        variables: dict[str, Any] = {"first": max(1, min(limit, 250))}
        if status:
            variables["query"] = f"status:{status}"
        result = self._graphql(_QUERY_PRODUCTS, variables)
        return result if "error" in result else _edges_nodes(result, "products")

    @staticmethod
    def _format_products(nodes: list[dict[str, Any]], *, inventory_view: bool) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = []
        for node in nodes:
            variants = _edges_nodes(node, "variants")
            if inventory_view:
                formatted.append(
                    {
                        "id": node["id"],
                        "title": node["title"],
                        "total_inventory": node.get("totalInventory"),
                        "tracks_inventory": node.get("tracksInventory"),
                        "variants": [
                            {
                                "id": v["id"],
                                "title": v["title"],
                                "sku": v.get("sku"),
                                "inventory_quantity": v.get("inventoryQuantity"),
                                "inventory_policy": v.get("inventoryPolicy"),
                            }
                            for v in variants
                        ],
                    },
                )
            else:
                price = node.get("priceRangeV2", {})
                formatted.append(
                    {
                        "id": node["id"],
                        "title": node["title"],
                        "status": node.get("status"),
                        "total_inventory": node.get("totalInventory"),
                        "created_at": node.get("createdAt"),
                        "price_range": {
                            "min": price.get("minVariantPrice", {}).get("amount"),
                            "max": price.get("maxVariantPrice", {}).get("amount"),
                            "currency": price.get("minVariantPrice", {}).get("currencyCode"),
                        },
                        "variants": [
                            {
                                "id": v["id"],
                                "title": v["title"],
                                "sku": v.get("sku"),
                                "price": v.get("price"),
                                "inventory": v.get("inventoryQuantity"),
                            }
                            for v in variants
                        ],
                    },
                )
        return formatted

    def _low_stock(self, threshold: int, max_results: int) -> Any:  # noqa: ANN401
        nodes = self._fetch_products(limit=250, status="ACTIVE")
        if isinstance(nodes, dict):
            return nodes
        low_stock: list[dict[str, Any]] = []
        for node in nodes:
            total_inv = node.get("totalInventory", 0)
            if total_inv is None or total_inv > threshold:
                continue
            variants = [
                {
                    "id": v["id"],
                    "title": v["title"],
                    "sku": v.get("sku"),
                    "inventory_quantity": v.get("inventoryQuantity"),
                }
                for v in _edges_nodes(node, "variants")
                if v.get("inventoryQuantity") is not None and v["inventoryQuantity"] <= threshold
            ]
            if variants:
                low_stock.append(
                    {
                        "id": node["id"],
                        "title": node["title"],
                        "total_inventory": total_inv,
                        "low_stock_variants": variants,
                    },
                )
        low_stock.sort(key=lambda item: item["total_inventory"])
        return low_stock[:max_results]

    def shopify_store(
        self,
        action: _StoreAction,
        max_results: int = 50,
        status: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        threshold: int = 10,
    ) -> str:
        """Read Shopify store metadata, products, orders, and inventory levels.

        Actions:
        - `shop_info`: Store metadata, currency, domain, and plan.
        - `products`: List products (optional `max_results`, `status`: ACTIVE/ARCHIVED/DRAFT).
        - `orders`: Recent orders (optional `max_results`, `status`: paid/pending/refunded, `created_after`/`created_before` YYYY-MM-DD).
        - `inventory_levels`: Inventory quantities for active products (`max_results`).
        - `low_stock`: Active products at or below `threshold` inventory (`max_results`).
        """
        if action == "shop_info":
            res = self._graphql(_QUERY_SHOP_INFO)
            return json_dumps(res if "error" in res else res.get("shop", {}))
        if action in {"products", "inventory_levels"}:
            nodes = self._fetch_products(limit=max_results, status="ACTIVE" if action == "inventory_levels" else status)
            return json_dumps(
                nodes
                if isinstance(nodes, dict)
                else self._format_products(nodes, inventory_view=action == "inventory_levels"),
            )
        if action == "low_stock":
            return json_dumps(self._low_stock(threshold, max_results))
        q = _build_order_query(status=status, created_after=created_after, created_before=created_before)
        orders = self._fetch_orders("orders", limit=max_results, query_filter=q, reverse=True)
        if isinstance(orders, dict):
            return json_dumps(orders)
        return json_dumps(
            [
                {
                    "id": o["id"],
                    "name": o["name"],
                    "created_at": o["createdAt"],
                    "financial_status": o.get("displayFinancialStatus"),
                    "fulfillment_status": o.get("displayFulfillmentStatus"),
                    "total": o.get("totalPriceSet", {}).get("shopMoney", {}).get("amount"),
                    "subtotal": o.get("subtotalPriceSet", {}).get("shopMoney", {}).get("amount"),
                    "currency": _shop_currency(o),
                    "customer": (
                        {
                            "id": o["customer"].get("id"),
                            "email": o["customer"].get("email"),
                            "name": _customer_name(o["customer"]),
                        }
                        if o.get("customer")
                        else None
                    ),
                    "line_items": [
                        {
                            "id": item.get("id"),
                            "title": item["title"],
                            "quantity": item["quantity"],
                            "unit_price": item.get("originalUnitPriceSet", {}).get("shopMoney", {}).get("amount"),
                            "sku": item.get("variant", {}).get("sku") if item.get("variant") else None,
                        }
                        for item in _edges_nodes(o, "lineItems")
                    ],
                }
                for o in orders
            ],
        )

    @staticmethod
    def _top_selling(orders: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        sales: dict[str, dict[str, Any]] = {}
        for order in orders:
            for item in _edges_nodes(order, "lineItems"):
                product = (item.get("variant") or {}).get("product")
                if not product:
                    continue
                pid = product["id"]
                qty = int(item.get("quantity", 0))
                entry = sales.setdefault(
                    pid,
                    {"id": pid, "title": product["title"], "total_quantity": 0, "total_revenue": 0.0, "order_count": 0},
                )
                entry["total_quantity"] += qty
                entry["total_revenue"] += qty * _shop_amount(item, "originalUnitPriceSet")
                entry["order_count"] += 1
        ranked = sorted(sales.values(), key=lambda item: item["total_quantity"], reverse=True)[:limit]
        for idx, product in enumerate(ranked, start=1):
            product["rank"] = idx
            product["total_revenue"] = round(product["total_revenue"], 2)
        return ranked

    @staticmethod
    def _bought_together(orders: list[dict[str, Any]], min_occurrences: int, limit: int) -> list[dict[str, Any]]:
        pair_counter: Counter[tuple[str, str]] = Counter()
        names: dict[str, str] = {}
        for order in orders:
            pids: set[str] = set()
            for item in _edges_nodes(order, "lineItems"):
                product = (item.get("variant") or {}).get("product")
                if product:
                    pids.add(product["id"])
                    names[product["id"]] = product["title"]
            pair_counter.update(combinations(sorted(pids), 2))
        return [
            {
                "product_1": {"id": p1, "title": names.get(p1, "Unknown")},
                "product_2": {"id": p2, "title": names.get(p2, "Unknown")},
                "times_bought_together": count,
            }
            for (p1, p2), count in pair_counter.most_common(limit)
            if count >= min_occurrences
        ]

    @staticmethod
    def _sales_by_date(orders: list[dict[str, Any]], start: str, end: str) -> dict[str, Any]:
        total_revenue = 0.0
        total_items = 0
        currency: str | None = None
        daily: dict[str, dict[str, Any]] = {}
        for order in orders:
            amount = _shop_amount(order, "totalPriceSet")
            currency = _shop_currency(order) or currency
            day = str(order["createdAt"])[:10]
            qty = sum(int(item.get("quantity", 0)) for item in _edges_nodes(order, "lineItems"))
            total_revenue += amount
            total_items += qty
            bucket = daily.setdefault(day, {"date": day, "revenue": 0.0, "orders": 0, "items": 0})
            bucket["revenue"] += amount
            bucket["orders"] += 1
            bucket["items"] += qty
        sorted_daily = [
            {**row, "revenue": round(row["revenue"], 2)} for row in sorted(daily.values(), key=lambda r: r["date"])
        ]
        count = len(orders)
        return {
            "period": {"start": start, "end": end},
            "total_revenue": round(total_revenue, 2),
            "total_orders": count,
            "total_items_sold": total_items,
            "average_order_value": round(total_revenue / count, 2) if count else 0,
            "currency": currency,
            "daily_breakdown": sorted_daily,
        }

    @staticmethod
    def _order_analytics(
        orders: list[dict[str, Any]],
        created_after: str | None,
        created_before: str | None,
    ) -> dict[str, Any]:
        if not orders:
            return {"message": "No orders found in the specified period"}
        values = [_shop_amount(o, "totalPriceSet") for o in orders]
        total_revenue = sum(values)
        total_items = sum(sum(int(i.get("quantity", 0)) for i in _edges_nodes(o, "lineItems")) for o in orders)
        count = len(orders)
        return {
            "period": {"created_after": created_after, "created_before": created_before},
            "total_orders": count,
            "total_revenue": round(total_revenue, 2),
            "total_subtotal": round(sum(_shop_amount(o, "subtotalPriceSet") for o in orders), 2),
            "total_shipping": round(sum(_shop_amount(o, "totalShippingPriceSet") for o in orders), 2),
            "total_tax": round(sum(_shop_amount(o, "totalTaxSet") for o in orders), 2),
            "currency": _shop_currency(orders[-1]),
            "average_order_value": round(total_revenue / count, 2),
            "total_items_sold": total_items,
            "average_items_per_order": round(total_items / count, 2),
            "min_order_value": round(min(values), 2),
            "max_order_value": round(max(values), 2),
            "financial_status_breakdown": dict(Counter(o.get("displayFinancialStatus", "UNKNOWN") for o in orders)),
            "fulfillment_status_breakdown": dict(Counter(o.get("displayFulfillmentStatus", "UNKNOWN") for o in orders)),
        }

    @staticmethod
    def _product_breakdown(
        orders: list[dict[str, Any]],
        product_id: str,
        created_after: str | None,
        created_before: str | None,
    ) -> dict[str, Any]:
        normalized = product_id if product_id.startswith("gid://") else f"gid://shopify/Product/{product_id}"
        product_title: str | None = None
        total_qty = 0
        total_rev = 0.0
        order_count = 0
        currency: str | None = None
        variants: dict[str, dict[str, Any]] = {}
        daily: dict[str, dict[str, Any]] = {}
        for order in orders:
            day = str(order["createdAt"])[:10]
            matched = False
            for item in _edges_nodes(order, "lineItems"):
                variant = item.get("variant") or {}
                product = variant.get("product") or {}
                if product.get("id") != normalized:
                    continue
                product_title = product.get("title")
                qty = int(item.get("quantity", 0))
                rev = qty * _shop_amount(item, "originalUnitPriceSet")
                currency = _shop_currency(item, "originalUnitPriceSet") or currency
                total_qty += qty
                total_rev += rev
                matched = True
                vid = variant["id"]
                vrow = variants.setdefault(
                    vid,
                    {
                        "variant_id": vid,
                        "variant_title": variant.get("title", "Default"),
                        "sku": variant.get("sku"),
                        "quantity": 0,
                        "revenue": 0.0,
                    },
                )
                vrow["quantity"] += qty
                vrow["revenue"] += rev
                drow = daily.setdefault(day, {"date": day, "quantity": 0, "revenue": 0.0})
                drow["quantity"] += qty
                drow["revenue"] += rev
            if matched:
                order_count += 1
        if product_title is None:
            return {"error": "Product not found in any orders during this period"}
        return {
            "product_id": normalized,
            "product_title": product_title,
            "period": {"created_after": created_after, "created_before": created_before},
            "total_quantity_sold": total_qty,
            "total_revenue": round(total_rev, 2),
            "order_count": order_count,
            "currency": currency,
            "average_units_per_order": round(total_qty / order_count, 2) if order_count else 0,
            "variant_breakdown": [{**r, "revenue": round(r["revenue"], 2)} for r in variants.values()],
            "daily_sales": [
                {**r, "revenue": round(r["revenue"], 2)} for r in sorted(daily.values(), key=lambda r: r["date"])
            ],
        }

    def _customer_orders(self, customer_email: str, limit: int) -> dict[str, Any]:
        orders = self._fetch_orders(
            "customer_orders",
            limit=limit,
            query_filter=_build_order_query(customer_email=customer_email),
            reverse=True,
        )
        if isinstance(orders, dict):
            return orders
        if not orders:
            return {"message": f"No orders found for {customer_email}"}
        cust = orders[0].get("customer") or {}
        return {
            "customer": {
                "email": customer_email,
                "id": cust.get("id"),
                "name": _customer_name(cust),
                "total_orders": cust.get("numberOfOrders"),
                "total_spent": cust.get("amountSpent", {}).get("amount"),
                "currency": cust.get("amountSpent", {}).get("currencyCode"),
            },
            "orders": [
                {
                    "id": o["id"],
                    "name": o["name"],
                    "created_at": o["createdAt"],
                    "financial_status": o.get("displayFinancialStatus"),
                    "fulfillment_status": o.get("displayFulfillmentStatus"),
                    "total": o.get("totalPriceSet", {}).get("shopMoney", {}).get("amount"),
                    "items": [{"title": i["title"], "quantity": i["quantity"]} for i in _edges_nodes(o, "lineItems")],
                }
                for o in orders
            ],
        }

    def _period_summary(self, start: str, end: str) -> dict[str, Any]:
        q = _build_order_query(paid_only=True, created_after=start, created_before=end, before_exclusive=True)
        orders = self._fetch_orders("sales_trends", limit=250, query_filter=q)
        if isinstance(orders, dict):
            return orders
        revenue = sum(_shop_amount(o, "totalPriceSet") for o in orders)
        items = sum(sum(int(i.get("quantity", 0)) for i in _edges_nodes(o, "lineItems")) for o in orders)
        return {
            "total_orders": len(orders),
            "total_revenue": round(revenue, 2),
            "total_items_sold": items,
            "average_order_value": round(revenue / len(orders), 2) if orders else 0,
            "currency": _shop_currency(orders[0]) if orders else None,
        }

    def _sales_trends(
        self,
        created_after: str | None,
        created_before: str | None,
        compare_previous_period: bool,
    ) -> dict[str, Any]:
        end_dt = (
            datetime.strptime(created_before, "%Y-%m-%d").replace(tzinfo=UTC) if created_before else datetime.now(UTC)
        )
        start_dt = (
            datetime.strptime(created_after, "%Y-%m-%d").replace(tzinfo=UTC)
            if created_after
            else end_dt - timedelta(days=30)
        )
        cur_start, cur_end = start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")
        cur = self._period_summary(cur_start, cur_end)
        if "error" in cur:
            return cur
        payload: dict[str, Any] = {"current_period": {"start": cur_start, "end": cur_end, **cur}}
        if compare_previous_period:
            prev_end_dt = start_dt - timedelta(days=1)
            prev_start_dt = prev_end_dt - timedelta(days=(end_dt - start_dt).days)
            prev_start, prev_end = prev_start_dt.strftime("%Y-%m-%d"), prev_end_dt.strftime("%Y-%m-%d")
            prev = self._period_summary(prev_start, prev_end)
            if "error" not in prev:
                payload["previous_period"] = {"start": prev_start, "end": prev_end, **prev}
                rev_chg = _pct_change(cur["total_revenue"], prev["total_revenue"])
                ord_chg = _pct_change(cur["total_orders"], prev["total_orders"])
                payload["comparison"] = {
                    "revenue_change_percent": round(rev_chg, 2),
                    "orders_change_percent": round(ord_chg, 2),
                    "revenue_trend": _trend_label(rev_chg),
                    "orders_trend": _trend_label(ord_chg),
                }
        return payload

    @staticmethod
    def _average_order_value(
        orders: list[dict[str, Any]],
        group_by: _GroupBy,
        created_after: str | None,
        created_before: str | None,
    ) -> dict[str, Any]:
        if not orders:
            return {"message": "No orders found in the specified period"}
        grouped: dict[str, list[float]] = {}
        currency: str | None = None
        for order in orders:
            day = str(order["createdAt"])[:10]
            currency = _shop_currency(order) or currency
            if group_by == "week":
                iso = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).isocalendar()
                key = f"{iso[0]}-W{iso[1]:02d}"
            else:
                key = day[:7] if group_by == "month" else day
            grouped.setdefault(key, []).append(_shop_amount(order, "totalPriceSet"))
        breakdown = [
            {
                "period": key,
                "order_count": len(vals),
                "total_revenue": round(sum(vals), 2),
                "average_order_value": round(sum(vals) / len(vals), 2),
            }
            for key, vals in sorted(grouped.items())
        ]
        total_rev = sum(sum(vals) for vals in grouped.values())
        total_cnt = sum(len(vals) for vals in grouped.values())
        return {
            "period": {"created_after": created_after, "created_before": created_before},
            "group_by": group_by,
            "overall_average_order_value": round(total_rev / total_cnt, 2) if total_cnt else 0,
            "currency": currency,
            "breakdown": breakdown,
        }

    @staticmethod
    def _repeat_customers(
        orders: list[dict[str, Any]],
        min_orders: int,
        limit: int,
        created_after: str | None,
        created_before: str | None,
    ) -> dict[str, Any]:
        customers: dict[str, dict[str, Any]] = {}
        for order in orders:
            cust = order.get("customer") or {}
            cid = cust.get("id")
            if not cid:
                continue
            row = customers.setdefault(
                cid,
                {
                    "id": cid,
                    "email": cust.get("email"),
                    "name": _customer_name(cust),
                    "total_orders_all_time": cust.get("numberOfOrders"),
                    "total_spent_all_time": cust.get("amountSpent", {}).get("amount"),
                    "currency": cust.get("amountSpent", {}).get("currencyCode"),
                    "orders_in_period": 0,
                    "spent_in_period": 0.0,
                },
            )
            row["orders_in_period"] += 1
            row["spent_in_period"] += _shop_amount(order, "totalPriceSet")
        repeat = [
            {**row, "spent_in_period": round(row["spent_in_period"], 2)}
            for row in customers.values()
            if row["orders_in_period"] >= min_orders
        ]
        repeat.sort(key=lambda row: (row["orders_in_period"], row["spent_in_period"]), reverse=True)
        return {
            "period": {"created_after": created_after, "created_before": created_before},
            "min_orders_threshold": min_orders,
            "repeat_customer_count": len(repeat),
            "customers": repeat[:limit],
        }

    def _dispatch_analytics(
        self,
        action: _AnalyticsAction,
        created_after: str | None,
        created_before: str | None,
        product_id: str | None,
        customer_email: str | None,
        limit: int,
        min_occurrences: int,
        group_by: _GroupBy,
        compare_previous_period: bool,
    ) -> Any:  # noqa: ANN401
        if action == "customer_orders":
            return self._customer_orders(require_non_empty(customer_email, "customer_email"), limit)
        if action == "sales_trends":
            return self._sales_trends(created_after, created_before, compare_previous_period)

        if action == "sales_by_date":
            start = require_non_empty(created_after, "created_after")
            end = require_non_empty(created_before, "created_before")
            q = _build_order_query(paid_only=True, created_after=start, created_before=end)
            orders = self._fetch_orders("sales_by_date", limit=250, query_filter=q)
            return orders if isinstance(orders, dict) else self._sales_by_date(orders, start, end)

        q = _build_order_query(
            paid_only=action != "order_analytics",
            created_after=created_after,
            created_before=created_before,
        )
        orders = self._fetch_orders(action, limit=250, query_filter=q)
        if isinstance(orders, dict):
            return orders
        if action == "top_selling":
            result: Any = self._top_selling(orders, limit)
        elif action == "bought_together":
            result = self._bought_together(orders, min_occurrences, limit)
        elif action == "order_analytics":
            result = self._order_analytics(orders, created_after, created_before)
        elif action == "product_breakdown":
            result = self._product_breakdown(
                orders,
                require_non_empty(product_id, "product_id"),
                created_after,
                created_before,
            )
        elif action == "average_order_value":
            result = self._average_order_value(orders, group_by, created_after, created_before)
        else:
            result = self._repeat_customers(orders, min_occurrences, limit, created_after, created_before)
        return result

    def shopify_analytics(
        self,
        action: _AnalyticsAction,
        created_after: str | None = None,
        created_before: str | None = None,
        product_id: str | None = None,
        customer_email: str | None = None,
        limit: int = 20,
        min_occurrences: int = 2,
        group_by: _GroupBy = "day",
        compare_previous_period: bool = True,
    ) -> str:
        """Analyze Shopify sales, product performance, and customer order insights.

        Actions (optional `created_after`/`created_before` YYYY-MM-DD unless noted):
        - `top_selling`: Top products by quantity (`limit`).
        - `bought_together`: Frequent product pairs (`min_occurrences`, `limit`).
        - `sales_by_date`: Revenue and daily breakdown (requires `created_after`, `created_before`).
        - `order_analytics`: Totals, AOV, shipping, tax, and status breakdowns.
        - `product_breakdown`: Sales by variant and day for `product_id`.
        - `customer_orders`: Order history for `customer_email` (`limit`).
        - `sales_trends`: Period metrics with optional `compare_previous_period`.
        - `average_order_value`: AOV grouped by `group_by` (`day`/`week`/`month`).
        - `repeat_customers`: Customers with >= `min_occurrences` orders (`limit`).
        """
        try:
            return json_dumps(
                self._dispatch_analytics(
                    action,
                    created_after,
                    created_before,
                    product_id,
                    customer_email,
                    limit,
                    min_occurrences,
                    group_by,
                    compare_previous_period,
                ),
            )
        except ValueError as exc:
            return error_json(str(exc))


@register_tool_with_metadata(
    name="shopify",
    file_access=ToolFileAccess.NONE,
    display_name="Shopify",
    description="Analyze sales data, products, orders, and customer insights from your Shopify store",
    category=ToolCategory.INTEGRATIONS,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiShopify",
    icon_color="text-green-600",
    config_fields=[
        ConfigField(
            name="shop_name",
            env=("SHOPIFY_SHOP_NAME",),
            label="Shop Name",
            type="text",
            required=True,
            placeholder="my-store",
            description="Your Shopify store name (e.g., 'my-store' from my-store.myshopify.com).",
        ),
        ConfigField(
            name="access_token",
            env=("SHOPIFY_ACCESS_TOKEN",),
            label="Access Token",
            type="password",
            required=True,
            description="Shopify Admin API access token.",
        ),
        ConfigField(
            name="api_version",
            label="API Version",
            type="text",
            required=False,
            default="2025-10",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/location-commerce-and-home/#shopify",
    helper_text=(
        "Create and install new apps through the [Dev Dashboard]"
        "(https://shopify.dev/docs/apps/build/dev-dashboard/create-apps-using-dev-dashboard), "
        "then follow its authentication steps and store the Admin API access token here. "
        "Existing legacy custom apps can keep their token setup. "
        "This tool does not issue or refresh tokens."
    ),
    function_names=("shopify_store", "shopify_analytics"),
    action_scoped_grants=("shopify_store", "shopify_analytics"),
)
def shopify_tools() -> type[Toolkit]:
    """Return Shopify tools for e-commerce analytics."""
    return _ShopifyTools
