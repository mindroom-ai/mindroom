# Location, Commerce, & Home

Use these tools to search places, fetch weather data, analyze Shopify stores, and control Home Assistant devices.

## What This Page Covers

This page documents the built-in tools in the `location-commerce-and-home` group.
Use these tools when you need physical-world lookup data, store analytics, or smart home control.

## Tools On This Page

- \[`google_maps`\] - Google Maps place search, directions, geocoding, address validation, elevation, and timezone lookups.
- \[`openweather`\] - Current weather, multi-day forecast, air pollution, and location geocoding from OpenWeather.
- \[`shopify`\] - Shopify Admin API analytics for shop info, products, orders, customers, inventory, and sales trends.
- \[`homeassistant`\] - Home Assistant entity state queries, device control, scene activation, and generic service calls.

## Common Setup Notes

All four tools on this page are `requires_config`, so they only become available after the needed credentials or integration setup is present.
MindRoom validates inline overrides against the declared `config_fields`, and `type="password"` fields such as `key`, `api_key`, `access_token`, and `HOMEASSISTANT_TOKEN` must be stored through the dashboard or credential store instead of inline YAML.
`google_maps`, `openweather`, and `shopify` are standard credential-backed tools with no dedicated MindRoom integration routes in `src/mindroom/api/integrations.py`.
Their upstream Agno toolkits also support environment fallbacks through `GOOGLE_MAPS_API_KEY`, `OPENWEATHER_API_KEY`, `SHOPIFY_SHOP_NAME`, and `SHOPIFY_ACCESS_TOKEN`.
`homeassistant` is different because MindRoom ships a dedicated integration flow in `src/mindroom/api/homeassistant_integration.py` with both OAuth and long-lived-token setup paths.
`homeassistant` is also a shared-only integration, so it requires `worker_scope` to be unset or `shared`.
Like the Google OAuth tools and unlike `spotify`, `homeassistant` always stays local and is never proxied through worker sandbox routing.
Missing optional dependencies can auto-install at first use unless `MINDROOM_NO_AUTO_INSTALL_TOOLS=1` is set.

## \[`google_maps`\]

`google_maps` is the Google Maps toolkit for place search, routing, geocoding, address validation, and location metadata.

### What It Does

`google_maps` exposes `search_places()`, `get_directions()`, `validate_address()`, `geocode_address()`, `reverse_geocode()`, `get_distance_matrix()`, `get_elevation()`, and `get_timezone()`.
The upstream toolkit builds both a `googlemaps.Client` and a `google.maps.places_v1.PlacesClient`.
`search_places()` returns rich place details including name, formatted address, rating, reviews, phone number, website, and opening hours.
`validate_address()` uses Google's Address Validation API rather than normal geocoding.
MindRoom does not add extra runtime behavior on top of the upstream toolkit beyond metadata, dependency management, and credential storage.

### Configuration

| Option | Type       | Required | Default | Notes                                                                 |
| ------ | ---------- | -------- | ------- | --------------------------------------------------------------------- |
| `key`  | `password` | `no`     | `null`  | Google Maps API key stored through the dashboard or credential store. |

### Example

```
agents:
  local_guide:
    tools:
      - google_maps
```

```
search_places("coffee shops near Pike Place Market")
get_directions("Seattle, WA", "Portland, OR", mode="driving")
geocode_address("1600 Amphitheatre Parkway, Mountain View, CA")
reverse_geocode(47.6205, -122.3493)
validate_address("1600 Amphitheatre Pkwy, Mountain View, CA", region_code="US")
```

### Notes

- `key` is optional in MindRoom metadata only because the upstream toolkit can also read `GOOGLE_MAPS_API_KEY` from the runtime environment.
- In practice, the tool still needs a valid Google Maps API key before any call succeeds.
- If you plan to use `validate_address()`, enable the Address Validation API for the same Google Cloud project as the key.

## \[`openweather`\]

`openweather` is the OpenWeather toolkit for weather, forecast, air quality, and place geocoding.

### What It Does

`openweather` exposes `get_current_weather()`, `get_forecast()`, `get_air_pollution()`, and `geocode_location()`.
The weather, forecast, and air pollution methods geocode the requested location first and then query OpenWeather by latitude and longitude.
`units` controls whether the toolkit requests `standard`, `metric`, or `imperial` output from the API.
`get_forecast()` uses the 5-day forecast endpoint and caps the response to 40 three-hour entries.
MindRoom does not add custom behavior here beyond metadata, dependency management, and credential storage.

### Configuration

| Option                   | Type       | Required | Default  | Notes                                                                 |
| ------------------------ | ---------- | -------- | -------- | --------------------------------------------------------------------- |
| `api_key`                | `password` | `no`     | `null`   | OpenWeather API key stored through the dashboard or credential store. |
| `units`                  | `text`     | `no`     | `metric` | Measurement units passed to the OpenWeather API.                      |
| `enable_current_weather` | `boolean`  | `no`     | `true`   | Enable `get_current_weather()`.                                       |
| `enable_forecast`        | `boolean`  | `no`     | `true`   | Enable `get_forecast()`.                                              |
| `enable_air_pollution`   | `boolean`  | `no`     | `true`   | Enable `get_air_pollution()`.                                         |
| `enable_geocoding`       | `boolean`  | `no`     | `true`   | Enable `geocode_location()`.                                          |
| `all`                    | `boolean`  | `no`     | `false`  | Enable the full OpenWeather toolkit.                                  |

### Example

```
agents:
  weather:
    tools:
      - openweather:
          units: imperial
          enable_air_pollution: false
```

```
get_current_weather("San Francisco")
get_forecast("Chicago", days=3)
get_air_pollution("Los Angeles")
geocode_location("Reykjavik", limit=3)
```

### Notes

- `api_key` is optional in MindRoom metadata only because the upstream toolkit can also read `OPENWEATHER_API_KEY` from the runtime environment.
- In practice, the tool still needs a valid OpenWeather API key before any call succeeds.
- Because weather lookups reuse the first geocoding match, ambiguous location names can resolve to an unexpected city unless you make the query more specific.

## \[`shopify`\]

`shopify` is the Shopify Admin GraphQL toolkit for store analytics, catalog inspection, order reporting, customer lookups, and inventory visibility.

### What It Does

`shopify` exposes `get_shop_info()`, `get_products()`, `get_orders()`, `get_top_selling_products()`, `get_products_bought_together()`, `get_sales_by_date_range()`, `get_order_analytics()`, `get_product_sales_breakdown()`, `get_customer_order_history()`, `get_inventory_levels()`, `get_low_stock_products()`, `get_sales_trends()`, `get_average_order_value()`, and `get_repeat_customers()`.
The toolkit talks to Shopify's Admin GraphQL endpoint at `https://<shop_name>.myshopify.com/admin/api/<api_version>/graphql.json`.
Most list-style methods cap query size to Shopify's first-page limits, such as 250 products or orders.
`get_orders()` supports `created_after`, `created_before`, and financial status filters, and the date filters expect `YYYY-MM-DD`.
MindRoom does not wrap the Shopify API further, so behavior comes directly from the upstream Agno toolkit.

### Configuration

| Option         | Type       | Required | Default   | Notes                                                                            |
| -------------- | ---------- | -------- | --------- | -------------------------------------------------------------------------------- |
| `shop_name`    | `text`     | `yes`    | `null`    | Store subdomain such as `my-store` from `my-store.myshopify.com`.                |
| `access_token` | `password` | `yes`    | `null`    | Shopify Admin API access token stored through the dashboard or credential store. |
| `api_version`  | `text`     | `no`     | `2025-10` | Shopify Admin API version used in GraphQL requests.                              |
| `timeout`      | `number`   | `no`     | `30`      | Request timeout in seconds.                                                      |

### Example

```
agents:
  store_analyst:
    tools:
      - shopify:
          shop_name: my-store
          api_version: 2025-10
          timeout: 45
```

```
get_shop_info()
get_products(max_results=25, status="ACTIVE")
get_orders(max_results=50, created_after="2026-03-01", created_before="2026-03-31")
get_low_stock_products(threshold=10)
get_average_order_value(days=30)
```

### Notes

- Create a custom app in Shopify Admin and grant the scopes you need before generating the access token.
- The upstream toolkit explicitly expects `read_orders`, `read_products`, `read_customers`, and `read_analytics` for its full analytics surface.
- `shop_name` and `access_token` can also come from `SHOPIFY_SHOP_NAME` and `SHOPIFY_ACCESS_TOKEN`, but MindRoom's documented configuration path is stored tool credentials.

## \[`homeassistant`\]

`homeassistant` is MindRoom's custom Home Assistant toolkit for entity state queries, device control, scenes, automations, and generic service calls.

### What It Does

`homeassistant` exposes `get_entity_state()`, `list_entities()`, `turn_on()`, `turn_off()`, `toggle()`, `set_brightness()`, `set_color()`, `set_temperature()`, `activate_scene()`, `trigger_automation()`, and `call_service()`.
The toolkit calls Home Assistant's REST API through `/api/states` and `/api/services/...`.
`list_entities()` returns a simplified response and limits the output to the first 50 entities to avoid huge payloads.
`set_brightness()` validates a `0` to `255` range, `set_color()` validates each RGB channel in the same range, and `call_service()` expects extra service data as a JSON string.
MindRoom adds important runtime behavior here by loading scoped credentials, enforcing shared-only integration rules, and returning a clear error when the agent's `worker_scope` does not allow the integration.

### Configuration

| Option                | Type       | Required | Default | Notes                                                                                                                                |
| --------------------- | ---------- | -------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `HOMEASSISTANT_URL`   | `url`      | `yes`    | `null`  | Dashboard field label for the Home Assistant base URL. The dedicated integration flow stores the normalized value as `instance_url`. |
| `HOMEASSISTANT_TOKEN` | `password` | `yes`    | `null`  | Dashboard field label for a long-lived access token. OAuth setup stores `access_token` and `refresh_token` instead.                  |

### Example

```
agents:
  home:
    worker_scope: shared
    tools:
      - homeassistant
```

```
list_entities("light")
get_entity_state("climate.thermostat")
turn_on("light.living_room")
set_brightness("light.living_room", 128)
activate_scene("scene.movie_time")
call_service("notify", "send_message", data='{"message": "Dinner is ready"}')
```

### Notes

- `homeassistant` requires `worker_scope` to be unset or `shared`, and it is unavailable for `worker_scope: user` or `worker_scope: user_agent`.
- `homeassistant`, `gmail`, `google_calendar`, `google_drive`, and `google_sheets` always stay local and are never proxied through the sandbox, even if you change `worker_tools`.
- The current setup path is the dedicated Home Assistant integration flow in the dashboard or `src/mindroom/api/homeassistant_integration.py`, not generic env-to-credentials syncing.
- That integration supports both OAuth and long-lived access tokens, and the OAuth flow requires a Home Assistant OAuth application with the callback URL `/api/homeassistant/callback` on the MindRoom dashboard host.
- The runtime tool itself looks for stored `instance_url` plus either `access_token` or `long_lived_token`, which is why tool availability checks differ from the raw metadata field names.

## Related Docs

- [Tools Overview](https://docs.mindroom.chat/tools/index.md)
- [Per-Agent Tool Configuration](https://docs.mindroom.chat/configuration/agents/#per-agent-tool-configuration)
- [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md)
