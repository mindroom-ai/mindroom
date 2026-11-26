---
icon: lucide/map-pin
---

# Location, Commerce, & Home

These tools let an agent look up places and weather, analyze a Shopify store, and control a Home Assistant smart home.

| Tool | Service | Functions |
| --- | --- | --- |
| [`google_maps`](#google_maps) | Google Maps | Place search, directions, geocoding, address validation, distances, elevation, timezones |
| [`openweather`](#openweather) | OpenWeather | Current weather, forecast, air pollution, location geocoding |
| [`shopify`](#shopify) | Shopify Admin API | Shop info, products, orders, customers, inventory, sales analytics |
| [`homeassistant`](#homeassistant) | Home Assistant | Entity states, device control, scenes, automations, service calls |

## Setup

Every tool on this page stays unavailable until its credentials are configured.
Store password fields such as `key`, `api_key`, and `access_token` through the dashboard or credential store, because they cannot be set inline in YAML (see [Security Restrictions](index.md#security-restrictions)).
Missing Python dependencies install automatically on first use (see [Automatic Dependency Installation](index.md#automatic-dependency-installation)).
`homeassistant` is connected through its own dashboard flow instead (see [Connecting Home Assistant](#connecting-home-assistant)).

## `google_maps`

`google_maps` provides `google_maps_places()` (`search_places`, `geocode`, `reverse_geocode`, `validate_address`, `elevation`, `timezone`) and `google_maps_routes()` (`directions`, `distance_matrix`).
`search_places` returns place details including name, address, rating, reviews, phone number, website, and opening hours.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `key` | `password` | `null` | Google Maps API key; falls back to `GOOGLE_MAPS_API_KEY`. |
| `search_places` | `boolean` | `true` | Enable `google_maps_places(action="search_places")`. |
| `get_directions` | `boolean` | `true` | Enable `google_maps_routes(action="directions")`. |
| `validate_address` | `boolean` | `true` | Enable `google_maps_places(action="validate_address")`. |
| `geocode_address` | `boolean` | `true` | Enable `google_maps_places(action="geocode")`. |
| `reverse_geocode` | `boolean` | `true` | Enable `google_maps_places(action="reverse_geocode")`. |
| `get_distance_matrix` | `boolean` | `true` | Enable `google_maps_routes(action="distance_matrix")`. |
| `get_elevation` | `boolean` | `true` | Enable `google_maps_places(action="elevation")`. |
| `get_timezone` | `boolean` | `true` | Enable `google_maps_places(action="timezone")`. |

Enable the Places API and Address Validation API in the same Google Cloud project as the API key when using `search_places` or `validate_address`.

```yaml
agents:
  local_guide:
    tools:
      - google_maps
```

```python
google_maps_places(action="search_places", query="coffee shops near Pike Place Market")
google_maps_routes(action="directions", origin="Seattle, WA", destination="Portland, OR", mode="driving")
google_maps_places(action="reverse_geocode", lat=47.6205, lng=-122.3493)
google_maps_places(action="validate_address", address="1600 Amphitheatre Pkwy, Mountain View, CA", region="US")
```

## `openweather`

`openweather` provides `openweather_get(action, location, days=5, limit=1)` with actions `current_weather`, `forecast`, `air_pollution`, and `geocode`.
The `forecast` action returns 3-hour entries for up to 5 days.
Weather lookups use the first geocoding match for the location name, so ambiguous names such as "Springfield" can resolve to an unexpected place; add a region or country to the query.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | OpenWeather API key; falls back to `OPENWEATHER_API_KEY`. A key is required for every call. |
| `units` | `text` | `metric` | `standard`, `metric`, or `imperial`. |
| `enable_current_weather` | `boolean` | `true` | Enable the `current_weather` action. |
| `enable_forecast` | `boolean` | `true` | Enable the `forecast` action. |
| `enable_air_pollution` | `boolean` | `true` | Enable the `air_pollution` action. |
| `enable_geocoding` | `boolean` | `true` | Enable the `geocode` action. |
| `all` | `boolean` | `false` | Enable all four actions regardless of the `enable_*` options. |
| `timeout` | `number` | `30` | Per-request timeout in seconds. |

```yaml
agents:
  weather:
    tools:
      - openweather:
          units: imperial
          enable_air_pollution: false
```

```python
openweather_get("current_weather", "San Francisco")
openweather_get("forecast", "Chicago", days=3)
openweather_get("geocode", "Reykjavik", limit=3)
```

## `shopify`

`shopify` provides `shopify_store()` (`shop_info`, `products`, `orders`, `inventory_levels`, `low_stock`) and `shopify_analytics()` (`top_selling`, `bought_together`, `sales_by_date`, `order_analytics`, `product_breakdown`, `customer_orders`, `sales_trends`, `average_order_value`, `repeat_customers`).
List functions return at most 250 products or orders per call.
Analytics such as sales totals, trends, and average order value use at most 250 matching orders per query, so results for a busy store or a long date range can be incomplete.
Date filters such as `created_after` and `created_before` take `YYYY-MM-DD`.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `shop_name` | `text` | `null` | Required. Store subdomain, such as `my-store` from `my-store.myshopify.com`. Falls back to `SHOPIFY_SHOP_NAME`. |
| `access_token` | `password` | `null` | Required. Admin API access token. Falls back to `SHOPIFY_ACCESS_TOKEN`. |
| `api_version` | `text` | `2025-10` | Shopify Admin API version. |
| `timeout` | `number` | `30` | Request timeout in seconds. |

To get an access token:

1. Create and install an app through the [Shopify Dev Dashboard](https://shopify.dev/docs/apps/build/dev-dashboard/create-apps-using-dev-dashboard) with the `read_orders`, `read_products`, `read_customers`, and `read_analytics` scopes.
   Shopify returns only the last 60 days of orders unless the app also has the approved `read_all_orders` scope, so request it for older sales reports.
2. Obtain an Admin API access token through Shopify's authentication flow; for stores in your own organization, use the [client credentials grant](https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant), whose tokens expire after 24 hours.
3. Store the token as `access_token` through the dashboard or credential store.

MindRoom does not issue or refresh Shopify tokens, so replace an expired token yourself.
[Existing legacy custom apps](https://changelog.shopify.com/posts/legacy-custom-apps-can-t-be-created-after-january-1-2026) keep working with their tokens, but new ones cannot be created in Shopify Admin.

```yaml
agents:
  store_analyst:
    tools:
      - shopify:
          shop_name: my-store
          timeout: 45
```

```python
shopify_store(action="products", max_results=25, status="ACTIVE")
shopify_store(action="orders", max_results=50, created_after="2026-03-01", created_before="2026-03-31")
shopify_store(action="low_stock", threshold=10)
shopify_analytics(action="average_order_value", group_by="day", created_after="2026-03-01", created_before="2026-03-31")
```

## `homeassistant`

<video controls playsinline preload="metadata" aria-label="A voice note from a phone locks up, turns off the lights, and lowers the heating" style="width: 100%" poster="https://github.com/user-attachments/assets/534252f8-c599-4a1c-9aef-f1131ef9dfaf" data-poster-light="https://github.com/user-attachments/assets/534252f8-c599-4a1c-9aef-f1131ef9dfaf" data-poster-dark="https://github.com/user-attachments/assets/6729cad6-62ae-4ea4-92f4-6a3e422e7c36">
  <source src="https://github.com/user-attachments/assets/bf32f178-1c18-4643-a2d6-1450e306cbe0" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/30f0c744-ecaf-4fbe-9c95-08576425ab3f" type="video/mp4">
</video>

`homeassistant` provides `get_entity_state()`, `list_entities()`, `turn_on()`, `turn_off()`, `toggle()`, `set_brightness()`, `set_color()`, `set_temperature()`, `activate_scene()`, `trigger_automation()`, and `call_service()`.
`list_entities()` returns at most 50 entities, so pass a domain such as `light` or `sensor` to narrow it.
`set_brightness()` takes `0` to `255`, `set_color()` takes RGB channels in the same range, and `call_service()` takes extra service data as a JSON string.
`set_temperature()` uses the temperature unit configured in Home Assistant.

`homeassistant` requires `worker_scope` unset or `shared` (see [Shared-only integrations](../deployment/sandbox-proxy.md#shared-only-integrations)).

```yaml
agents:
  home:
    tools:
      - homeassistant
```

```python
list_entities("light")
get_entity_state("climate.thermostat")
set_brightness("light.living_room", 128)
activate_scene("scene.movie_time")
call_service("notify", "send_message", data='{"message": "Dinner is ready"}')
```

### Connecting Home Assistant

Connect Home Assistant in the dashboard's **Tools** tab, using either method:

- **OAuth**: enter the Home Assistant URL; Home Assistant needs no application registration, and after sign-in it returns you to `MINDROOM_DASHBOARD_URL` when you connect from that host, else to `MINDROOM_PUBLIC_URL`, or to the address MindRoom received the request on when that is unset, so set `MINDROOM_PUBLIC_URL` when a proxy in front of MindRoom terminates HTTPS.
  Home Assistant refuses that address with `Invalid client id` when it is an IP address other than a loopback, private, or link-local one, such as a public or Tailscale `100.x` address, so set `MINDROOM_PUBLIC_URL`, or `MINDROOM_DASHBOARD_URL` when you connect from that host, to a URL with a DNS name, such as `https://mindroom.example.org`, or use the access-token method.
- **Access token**: in your Home Assistant profile, create a long-lived access token under **Long-Lived Access Tokens**, then enter the Home Assistant URL and the token.

Expired OAuth access tokens renew automatically.
A Home Assistant URL on a private, local, or loopback network, such as `http://homeassistant.local:8123`, is rejected unless you enable **Allow private or local URL**; enable it only for a trusted self-hosted instance.

| Error | Fix |
| --- | --- |
| `Home Assistant is not configured. Please connect through the dashboard.` | Connect Home Assistant as above. |
| `Invalid authentication token. Please reconnect Home Assistant.` | The token was revoked or OAuth renewal failed; reconnect. |
| `Private Home Assistant URLs require explicit opt-in. ...` | Enable **Allow private or local URL** and reconnect. |
| `Connection timeout - check if Home Assistant is accessible` | Make sure the MindRoom server can reach the Home Assistant URL. |
| `Invalid client id` on Home Assistant's sign-in page | Give MindRoom's address a DNS name, as the **OAuth** method above describes, or connect with an access token. |

## Related Docs

- [Tools Overview](index.md)
- [Per-Agent Tool Configuration](index.md#per-agent-tool-configuration)
