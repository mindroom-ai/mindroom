# Integrations OAuth cards: one assembly path for built-in and discovered providers

This record holds the decisions behind assembling every OAuth card on the dashboard Integrations page in one loop.
It records reasons, rejected alternatives, and accepted limitations; the code shows the mechanisms.

## Decisions

- Built-in cards remain as descriptors, but only for what discovery cannot supply: the Google cards' written names, descriptions, categories, and icons, Google Cloud's name (its only tool is BigQuery, so discovery would call the card "Google BigQuery"), and visibility whatever the tools list returns, including before it loads.
  Every other built-in OAuth provider (GitHub, Slack, Atlassian, Spotify, the Microsoft tools) already appears through discovery, and plugins add providers the same way.
- Home Assistant stays its own provider with its own status endpoint, setup dialog, and disconnect; it joins the same card loop as one more built-in card, and the provider resolver names it explicitly.
- The tool that stands for a provider's card is the tool named after the provider, else the provider's only tool.
  This rule already held for built-in cards and is the only way Home Assistant's tool (which has no `auth_provider`) reaches its card; discovered cards now follow it too.
- A provider's card owns its connection state through its status alone; providers report a status for every card, and the card descriptors carry none.
- Providers hold behavior only (connect, disconnect, status, setup component); the assembled card is the one copy of display data, so the setup dialog reads the clicked card.

## Rejected alternatives

- **Derive every card from discovery and drop the Google descriptors**: cards would wait for the tools list and vanish when it fails, Google Cloud would be renamed, and the Google texts would change.
- **Keep a provider registry for Home Assistant only**: two ways to pick a provider for one special case.
- **Load provider statuses in parallel**: a separate performance change; statuses still load one after another, and cards appear once all have loaded.
- **Render built-in icons from tool icon names**: `SiGooglecloud` and `SiHomeassistant` are not in the generated icon map.

## Accepted behavior changes

- A discovered provider's card takes the tool named after the provider even when another tool of that provider is listed first, or when the same-named tool belongs to no provider, because the tool cards hide every tool named after a provider and its settings would otherwise be unreachable.
  No shipped tool metadata has this shape.
- When an isolating scope hides a shared-only provider such as Spotify, its tools are filtered like Home Assistant's: hidden from the tool cards unless several tools share the provider without a same-named tool.
  The backend already marks shared-only tools unsupported in those scopes, so the shipped Spotify tool was hidden either way.
