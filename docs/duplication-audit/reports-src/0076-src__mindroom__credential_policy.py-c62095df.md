## Summary

No meaningful duplication found.
`src/mindroom/credential_policy.py` is already the shared source for worker credential placement, OAuth client-config service naming, dashboard OAuth editability, OAuth token-shape detection, and OAuth credential-field filtering.
Nearby code in `src/mindroom/api/credentials.py` composes these helpers with dashboard-specific HTTP errors and OAuth registry matches, but does not duplicate the full policy behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
CredentialServicePolicy	class	lines 61-70	none-found	CredentialServicePolicy dataclass credential placement policy fields	src/mindroom/credentials.py:475, src/mindroom/api/credentials.py:570, src/mindroom/api/tools.py:71
credential_service_policy	function	lines 73-88	none-found	credential_service_policy worker_scope local shared primary runtime worker_grantable_supported	src/mindroom/credentials.py:384, src/mindroom/credentials.py:475, src/mindroom/config/models.py:456, src/mindroom/workers/backends/kubernetes.py:265, src/mindroom/api/credentials.py:570, src/mindroom/api/tools.py:71
is_oauth_client_config_service	function	lines 91-93	none-found	endswith _oauth_client client config service suffix	src/mindroom/oauth/providers.py:277, src/mindroom/oauth/providers.py:285, src/mindroom/oauth/providers.py:293, src/mindroom/api/credentials.py:166, src/mindroom/api/credentials.py:680
dashboard_may_edit_oauth_service	function	lines 96-98	related-only	dashboard may edit oauth token_service tool_config_service client_config_service	src/mindroom/api/credentials.py:665, src/mindroom/api/credentials.py:169, src/mindroom/oauth/registry.py:123
looks_like_oauth_credentials	function	lines 101-108	none-found	_source oauth _oauth_provider _id_token _oauth_claims token shape	src/mindroom/api/credentials.py:85, src/mindroom/api/credentials.py:688, src/mindroom/oauth/providers.py:172, src/mindroom/oauth/providers.py:203, src/mindroom/api/oauth.py:252
filter_oauth_credential_fields	function	lines 111-117	related-only	OAUTH_CREDENTIAL_FIELDS filter oauth fields startswith underscore	src/mindroom/api/credentials.py:77, src/mindroom/api/credentials.py:82, src/mindroom/api/credentials.py:749, src/mindroom/api/credentials.py:1161
```

## Findings

No real duplication found.

Related-only candidates:

- `src/mindroom/api/credentials.py:665` wraps `dashboard_may_edit_oauth_service()` with dashboard-specific match handling.
  It treats registered client-config services as editable before delegating token/tool config roles to the policy helper.
  That extra branch depends on `OAuthCredentialServiceMatch.client_config_service`, so it should remain local to API credential access.
- `src/mindroom/api/credentials.py:77` and `src/mindroom/api/credentials.py:1161` also remove underscore-prefixed metadata.
  This overlaps with the underscore-removal part of `filter_oauth_credential_fields()` but serves broader dashboard response/copy behavior where OAuth field removal is not always desired.
  The difference to preserve is that `filter_oauth_credential_fields()` removes both OAuth token fields and all internal fields, while these local comprehensions remove only internal metadata.

Centralized reuse already present:

- Worker grantability and storage placement use `credential_service_policy()` in `src/mindroom/credentials.py:384`, `src/mindroom/credentials.py:475`, `src/mindroom/config/models.py:456`, `src/mindroom/workers/backends/kubernetes.py:265`, `src/mindroom/api/credentials.py:570`, and `src/mindroom/api/tools.py:71`.
- OAuth client-config suffix checks use `is_oauth_client_config_service()` in `src/mindroom/oauth/providers.py:277`, `src/mindroom/oauth/providers.py:285`, `src/mindroom/oauth/providers.py:293`, and `src/mindroom/api/credentials.py:166`.
- OAuth token-shape detection and token-field filtering use `looks_like_oauth_credentials()` and `filter_oauth_credential_fields()` from `src/mindroom/api/credentials.py:82` and `src/mindroom/api/credentials.py:687`.

## Proposed Generalization

No refactor recommended.
The remaining similar comprehensions in `src/mindroom/api/credentials.py` are narrower dashboard helpers, not duplicate OAuth policy.

## Risk/tests

No production code was changed.
If future refactoring extracts an internal-metadata filter, tests should cover dashboard credential response filtering, credential copy behavior, OAuth token credential rejection, OAuth client-config response filtering, and worker credential allowlist validation.
