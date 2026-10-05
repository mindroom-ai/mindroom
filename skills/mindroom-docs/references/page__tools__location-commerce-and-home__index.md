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
Store password fields such as `key`, `api_key`, and `access_token` through the dashboard or credential store, because they cannot be set inline in YAML (see [Security Restrictions](https://docs.mindroom.chat/tools/#security-restrictions)).
Missing Python dependencies install automatically on first use (see [Automatic Dependency Installation](https://docs.mindroom.chat/tools/#automatic-dependency-installation)).
`homeassistant` is connected through its own dashboard flow instead (see [Connecting Home Assistant](#connecting-home-assistant)).

## `google_maps`

`google_maps` provides `search_places()`, `get_directions()`, `validate_address()`, `geocode_address()`, `reverse_geocode()`, `get_distance_matrix()`, `get_elevation()`, and `get_timezone()`.
`search_places()` returns place details including name, address, rating, reviews, phone number, website, and opening hours.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `key` | `password` | `null` | Google Maps API key; falls back to the `GOOGLE_MAPS_API_KEY` process environment variable. |
| `search_places` | `boolean` | `true` | Enable `search_places()`. |
| `get_directions` | `boolean` | `true` | Enable `get_directions()`. |
| `validate_address` | `boolean` | `true` | Enable `validate_address()`. |
| `geocode_address` | `boolean` | `true` | Enable `geocode_address()`. |
| `reverse_geocode` | `boolean` | `true` | Enable `reverse_geocode()`. |
| `get_distance_matrix` | `boolean` | `true` | Enable `get_distance_matrix()`. |
| `get_elevation` | `boolean` | `true` | Enable `get_elevation()`. |
| `get_timezone` | `boolean` | `true` | Enable `get_timezone()`. |

The toolkit needs Google Application Default Credentials for place search in addition to the API key, so configure both.
`validate_address()` uses Google's Address Validation API, which must be enabled in the same Google Cloud project as the key.

```yaml
agents:
  local_guide:
    tools:
      - google_maps
```

```python
search_places("coffee shops near Pike Place Market")
get_directions("Seattle, WA", "Portland, OR", mode="driving")
reverse_geocode(47.6205, -122.3493)
validate_address("1600 Amphitheatre Pkwy, Mountain View, CA", region_code="US")
```

## `openweather`

`openweather` provides `get_current_weather()`, `get_forecast()`, `get_air_pollution()`, and `geocode_location()`.
`get_forecast()` returns 3-hour entries for up to 5 days.
Weather lookups use the first geocoding match for the location name, so ambiguous names such as "Springfield" can resolve to an unexpected place; add a region or country to the query.

| Option | Type | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `password` | `null` | OpenWeather API key; falls back to the `OPENWEATHER_API_KEY` process environment variable. A key is required for every call. |
| `units` | `text` | `metric` | `standard`, `metric`, or `imperial`. |
| `enable_current_weather` | `boolean` | `true` | Enable `get_current_weather()`. |
| `enable_forecast` | `boolean` | `true` | Enable `get_forecast()`. |
| `enable_air_pollution` | `boolean` | `true` | Enable `get_air_pollution()`. |
| `enable_geocoding` | `boolean` | `true` | Enable `geocode_location()`. |
| `all` | `boolean` | `false` | Enable all four functions regardless of the `enable_*` options. |
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
get_current_weather("San Francisco")
get_forecast("Chicago", days=3)
geocode_location("Reykjavik", limit=3)
```

## `shopify`

`shopify` provides `get_shop_info()`, `get_products()`, `get_orders()`, `get_top_selling_products()`, `get_products_bought_together()`, `get_sales_by_date_range()`, `get_order_analytics()`, `get_product_sales_breakdown()`, `get_customer_order_history()`, `get_inventory_levels()`, `get_low_stock_products()`, `get_sales_trends()`, `get_average_order_value()`, and `get_repeat_customers()`.
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
get_products(max_results=25, status="ACTIVE")
get_orders(max_results=50, created_after="2026-03-01", created_before="2026-03-31")
get_low_stock_products(threshold=10)
get_average_order_value(group_by="day", created_after="2026-03-01", created_before="2026-03-31")
```

## `homeassistant`

<video controls playsinline preload="metadata" aria-label="A voice note from a phone locks up, turns off the lights, and lowers the heating" style="width: 100%" poster="https://github.com/user-attachments/assets/e7b9f61d-45c0-4e64-81d5-2a68141de523" data-poster-light="https://github.com/user-attachments/assets/e7b9f61d-45c0-4e64-81d5-2a68141de523" data-poster-dark="https://github.com/user-attachments/assets/aac77c0a-6c51-4e8b-aaa6-e682ca08f228">
  <source src="https://github.com/user-attachments/assets/51d85cfc-79de-4666-a3e2-aa2356f1c5ed" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/b5d7a6f2-11bb-45a5-b92e-03ab0d9c5ba0" type="video/mp4">
</video>

`homeassistant` provides `get_entity_state()`, `list_entities()`, `turn_on()`, `turn_off()`, `toggle()`, `set_brightness()`, `set_color()`, `set_temperature()`, `activate_scene()`, `trigger_automation()`, and `call_service()`.
`list_entities()` returns at most 50 entities, so pass a domain such as `light` or `sensor` to narrow it.
`set_brightness()` takes `0` to `255`, `set_color()` takes RGB channels in the same range, and `call_service()` takes extra service data as a JSON string.
`set_temperature()` uses the temperature unit configured in Home Assistant.

`homeassistant` requires `worker_scope` unset or `shared` (see [Shared-only integrations](https://docs.mindroom.chat/deployment/sandbox-proxy/#shared-only-integrations)).

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

- **OAuth**: enter the Home Assistant URL and, as the Client ID, the MindRoom dashboard URL, such as `https://mindroom.example.com`; Home Assistant needs no application registration, but the Client ID must use the same host and port as the dashboard you connect from.
- **Access token**: in your Home Assistant profile, create a long-lived access token under **Long-Lived Access Tokens**, then enter the Home Assistant URL and the token.

Expired OAuth access tokens renew automatically.
A Home Assistant URL on a private, local, or loopback network, such as `http://homeassistant.local:8123`, is rejected unless you enable **Allow private or local URL**; enable it only for a trusted self-hosted instance.

| Error | Fix |
| --- | --- |
| `Home Assistant is not configured. Please connect through the dashboard.` | Connect Home Assistant as above. |
| `Invalid authentication token. Please reconnect Home Assistant.` | The token was revoked or OAuth renewal failed; reconnect. |
| `Private Home Assistant URLs require explicit opt-in. ...` | Enable **Allow private or local URL** and reconnect. |
| `Connection timeout - check if Home Assistant is accessible` | Make sure the MindRoom server can reach the Home Assistant URL. |

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/tools/#per-agent-tool-configuration)
