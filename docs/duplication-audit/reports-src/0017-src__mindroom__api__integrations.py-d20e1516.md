Summary: `src/mindroom/api/integrations.py` contains a service-specific Spotify OAuth implementation that duplicates the generic provider OAuth API in `src/mindroom/api/oauth.py` and `src/mindroom/oauth/providers.py`.
The strongest duplication is the connect/callback/status/disconnect lifecycle.
Smaller active duplication exists in request-scoped credential load/save/delete helpers shared with Home Assistant and lower-level API credential helpers.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
get_dashboard_url	function	lines 29-31	related-only	get_dashboard_url dashboard base_url public base url oauth success	src/mindroom/api/homeassistant_integration.py:31; src/mindroom/api/homeassistant_integration.py:221; src/mindroom/api/homeassistant_integration.py:354; src/mindroom/oauth/service.py:151; src/mindroom/oauth/service.py:168
_get_spotify_redirect_uri	function	lines 34-39	duplicate-found	SPOTIFY_REDIRECT_URI redirect_uri default_redirect_uri MINDROOM_PUBLIC_URL callback	src/mindroom/oauth/providers.py:371; src/mindroom/oauth/service.py:151; src/mindroom/api/homeassistant_integration.py:221
_SpotifyClientProtocol	class	lines 42-43	related-only	SpotifyProtocol current_user Protocol spotipy Spotify src/mindroom/api/integrations.py:42; src/mindroom/tools/spotify.py:71; src/mindroom/api/auth.py:44
_SpotifyClientProtocol.current_user	method	lines 43-43	related-only	current_user get_current_user Spotify src/mindroom/api/integrations.py:133; src/mindroom/api/integrations.py:211; src/mindroom/tools/spotify.py:55
_SpotifyClientFactoryProtocol	class	lines 46-47	related-only	Spotify factory Protocol cast importlib spotipy src/mindroom/api/integrations.py:46; src/mindroom/api/auth.py:57; src/mindroom/workers/backends/kubernetes_resources.py:598
_SpotifyClientFactoryProtocol.__call__	method	lines 47-47	related-only	Protocol __call__ auth factory Spotify src/mindroom/api/integrations.py:47; src/mindroom/api/integrations.py:132; src/mindroom/api/integrations.py:210
_SpotifyOAuthClientProtocol	class	lines 50-53	related-only	SpotifyOAuth Protocol get_authorize_url get_access_token src/mindroom/api/integrations.py:50; src/mindroom/oauth/providers.py:386; src/mindroom/oauth/providers.py:419
_SpotifyOAuthClientProtocol.get_authorize_url	method	lines 51-51	duplicate-found	get_authorize_url authorization_uri create_authorization_url state scope	src/mindroom/api/integrations.py:170; src/mindroom/oauth/providers.py:386; src/mindroom/oauth/providers.py:410; src/mindroom/api/oauth.py:102
_SpotifyOAuthClientProtocol.get_access_token	method	lines 53-53	duplicate-found	get_access_token exchange_code fetch_token authorization_code	src/mindroom/api/integrations.py:207; src/mindroom/oauth/providers.py:419; src/mindroom/oauth/providers.py:456; src/mindroom/api/homeassistant_integration.py:323
_SpotifyOAuthFactoryProtocol	class	lines 56-64	related-only	SpotifyOAuth factory Protocol client_id client_secret redirect_uri scope	src/mindroom/api/integrations.py:56; src/mindroom/oauth/providers.py:65; src/mindroom/oauth/providers.py:248
_SpotifyOAuthFactoryProtocol.__call__	method	lines 57-64	duplicate-found	OAuth factory client_id client_secret redirect_uri scope OAuthClientConfig	src/mindroom/api/integrations.py:163; src/mindroom/api/integrations.py:201; src/mindroom/oauth/providers.py:65; src/mindroom/oauth/providers.py:394
_ensure_spotify_packages	function	lines 67-77	related-only	ensure_tool_deps importlib.import_module spotipy optional dependency	src/mindroom/api/integrations.py:67; src/mindroom/oauth/client.py:202; src/mindroom/oauth/client.py:288; src/mindroom/tools/openbb.py:20
SpotifyStatus	class	lines 80-85	related-only	status connected details error BaseModel OAuthStatusResponse HomeAssistantStatus	src/mindroom/api/oauth.py:60; src/mindroom/api/homeassistant_integration.py:40; src/mindroom/api/tools.py:48
_get_spotify_credentials	function	lines 88-92	duplicate-found	load credentials resolve_request_credentials_target load_credentials_for_target service helper	src/mindroom/api/homeassistant_integration.py:74; src/mindroom/api/homeassistant_integration.py:150; src/mindroom/api/credentials.py:540; src/mindroom/api/oauth.py:440
_save_spotify_credentials	function	lines 95-114	duplicate-found	save credentials _source ui resolve target target_manager.save_credentials scoped save	src/mindroom/api/homeassistant_integration.py:79; src/mindroom/api/credentials.py:592; src/mindroom/api/oauth.py:414
get_spotify_status	async_function	lines 118-143	duplicate-found	integration status connected credentials access_token current_user OAuth status usable	src/mindroom/api/oauth.py:435; src/mindroom/api/homeassistant_integration.py:147; src/mindroom/api/tools.py:256
connect_spotify	async_function	lines 148-171	duplicate-found	connect OAuth issue_pending_oauth_state authorization URL client config src/mindroom/api/oauth.py:102; src/mindroom/api/oauth.py:307; src/mindroom/api/homeassistant_integration.py:197
spotify_callback	async_function	lines 175-233	duplicate-found	OAuth callback verify_user consume_pending_oauth_request exchange code save credentials redirect src/mindroom/api/oauth.py:368; src/mindroom/api/homeassistant_integration.py:291; src/mindroom/oauth/providers.py:419
disconnect_spotify	async_function	lines 237-242	duplicate-found	disconnect delete credentials resolve_request_credentials_target delete_scoped_credentials	src/mindroom/api/oauth.py:485; src/mindroom/api/homeassistant_integration.py:360; src/mindroom/api/credentials.py:624
```

Findings:

1. Spotify duplicates the generic OAuth provider lifecycle.
   `connect_spotify` builds a client config from env, issues pending state, creates an authorization URL, and returns it at `src/mindroom/api/integrations.py:148`.
   The generic OAuth route does the same provider-neutral work in `_issue_authorization_url` and `connect` at `src/mindroom/api/oauth.py:102` and `src/mindroom/api/oauth.py:307`.
   `spotify_callback` validates state, verifies the user, consumes pending state, exchanges a code, stores credentials, and redirects at `src/mindroom/api/integrations.py:175`.
   The same lifecycle is implemented generically in `callback` at `src/mindroom/api/oauth.py:368`, backed by `OAuthProvider.exchange_code` at `src/mindroom/oauth/providers.py:419`.
   Differences to preserve: Spotify currently reads `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and optional `SPOTIFY_REDIRECT_URI` directly from runtime env, uses Spotipy rather than Authlib, stores `access_token` under that exact key for the Agno Spotify tool metadata in `src/mindroom/tools/spotify.py:24`, stores `username`, and redirects to `/?spotify=connected` rather than a provider success page.

2. Spotify credential load/save/delete wrappers duplicate request-target credential helpers.
   `_get_spotify_credentials`, `_save_spotify_credentials`, and `disconnect_spotify` resolve the request credential target and call load/save/delete for the hard-coded service at `src/mindroom/api/integrations.py:88`, `src/mindroom/api/integrations.py:95`, and `src/mindroom/api/integrations.py:237`.
   Home Assistant repeats the same service-specific pattern in `_get_stored_config`, `_save_config`, and `disconnect` at `src/mindroom/api/homeassistant_integration.py:74`, `src/mindroom/api/homeassistant_integration.py:79`, and `src/mindroom/api/homeassistant_integration.py:360`.
   Lower-level service-parameterized helpers already exist as `load_credentials_for_target`, `_save_credentials_for_target`, and `_delete_credentials_for_target` at `src/mindroom/api/credentials.py:540`, `src/mindroom/api/credentials.py:592`, and `src/mindroom/api/credentials.py:624`.
   Differences to preserve: `_save_spotify_credentials` always adds `_source: ui`; Home Assistant additionally normalizes `instance_url`; the private `_save_credentials_for_target` is not exported.

3. Spotify status duplicates provider status checks but has provider-specific account probing.
   `get_spotify_status` resolves credentials, treats an `access_token` as provisionally connected, optionally calls `current_user`, and returns details or an error at `src/mindroom/api/integrations.py:118`.
   Generic OAuth status resolves scoped credentials and derives `connected` through `oauth_credentials_usable` at `src/mindroom/api/oauth.py:435`.
   Home Assistant status follows the same "load credentials, probe external API, return connected/error details" shape at `src/mindroom/api/homeassistant_integration.py:147`.
   Differences to preserve: Spotify uses Spotipy `current_user` for username/email/product, and its tool metadata only requires `access_token`.

Proposed generalization:

1. No production refactor in this audit.
2. If refactoring later, register Spotify as an `OAuthProvider` or a small provider adapter that can use the generic `/api/oauth/{provider_id}` routes while preserving the stored `access_token` schema expected by `src/mindroom/tools/spotify.py`.
3. Move the `_source: ui` credential write pattern into an exported service-parameterized helper near `src/mindroom/api/credentials.py`, with an optional pre-save transform for Home Assistant URL normalization.
4. Keep provider-specific status probes separate unless there is a concrete need for rich account details across multiple providers.

Risk/tests:

Changing Spotify to the generic OAuth framework risks breaking existing dashboard callback URLs, the `SPOTIFY_REDIRECT_URI` override, and the `access_token` credential shape used by the Spotify tool.
Tests would need to cover Spotify connect URL generation, callback token storage, scoped credential targeting with `agent_name` and execution scope overrides, disconnect behavior, and status responses for missing, valid, and invalid access tokens.
No refactor is recommended as part of this audit because the generic OAuth framework already exists and the remaining work is provider migration, not a small local deduplication.
