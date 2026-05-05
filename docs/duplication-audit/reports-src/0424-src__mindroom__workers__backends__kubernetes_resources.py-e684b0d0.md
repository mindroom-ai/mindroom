## Summary

Top duplication candidates for `src/mindroom/workers/backends/kubernetes_resources.py`:

1. Worker identity/path derivation is split between Kubernetes resource names and worker storage directory names.
2. Dedicated runner runtime environment construction is repeated across Kubernetes pod manifests, startup manifests, and runner bootstrap/runtime overlay code.
3. User-agent private visibility checks are repeated in Kubernetes volume mounting and sandbox worker request preparation.

No production code was edited.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
DeploymentApplyResult	class	lines 67-70	not-a-behavior-symbol	result dataclass recreated	candidates: src/mindroom/workers/backends/kubernetes.py:397
_ApiStatusError	class	lines 73-74	not-a-behavior-symbol	ApiException status protocol	candidates: src/mindroom/workers/backends/kubernetes_resources.py:343,552,575
_KubernetesMetadata	class	lines 77-82	not-a-behavior-symbol	kubernetes metadata protocol annotations labels generation uid	candidates: src/mindroom/workers/backends/kubernetes.py:632
_KubernetesDeploymentSpec	class	lines 85-86	not-a-behavior-symbol	kubernetes deployment spec replicas protocol	candidates: src/mindroom/workers/backends/kubernetes.py:622
_KubernetesDeploymentStatus	class	lines 89-91	not-a-behavior-symbol	kubernetes deployment status ready_replicas observed_generation	candidates: src/mindroom/workers/backends/kubernetes.py:622
KubernetesDeployment	class	lines 94-99	not-a-behavior-symbol	deployment protocol metadata spec status	candidates: src/mindroom/workers/backends/kubernetes.py:622,632
_KubernetesPodSpec	class	lines 102-103	not-a-behavior-symbol	pod spec node_name protocol	candidates: src/mindroom/workers/backends/kubernetes_resources.py:932
_KubernetesPod	class	lines 106-107	not-a-behavior-symbol	pod protocol spec node_name	candidates: src/mindroom/workers/backends/kubernetes_resources.py:171
_KubernetesDeploymentList	class	lines 110-111	not-a-behavior-symbol	deployment list items protocol	candidates: src/mindroom/workers/backends/kubernetes_resources.py:335
_AppsApiProtocol	class	lines 114-128	not-a-behavior-symbol	apps api protocol deployment methods	candidates: src/mindroom/workers/backends/kubernetes_resources.py:318
_AppsApiProtocol.read_namespaced_deployment	method	lines 115-115	not-a-behavior-symbol	kubernetes read_namespaced_deployment	candidates: src/mindroom/workers/backends/kubernetes_resources.py:343
_AppsApiProtocol.create_namespaced_deployment	method	lines 117-117	not-a-behavior-symbol	kubernetes create_namespaced_deployment	candidates: src/mindroom/workers/backends/kubernetes_resources.py:486,552
_AppsApiProtocol.patch_namespaced_deployment	method	lines 119-124	not-a-behavior-symbol	kubernetes patch_namespaced_deployment	candidates: src/mindroom/workers/backends/kubernetes_resources.py:422,552
_AppsApiProtocol.delete_namespaced_deployment	method	lines 126-126	not-a-behavior-symbol	kubernetes delete_namespaced_deployment	candidates: src/mindroom/workers/backends/kubernetes_resources.py:440
_AppsApiProtocol.list_namespaced_deployment	method	lines 128-128	not-a-behavior-symbol	kubernetes list_namespaced_deployment label_selector	candidates: src/mindroom/workers/backends/kubernetes_resources.py:335
_KubernetesApiClientProtocol	class	lines 131-151	not-a-behavior-symbol	api_client call_api protocol	candidates: src/mindroom/workers/backends/kubernetes_resources.py:462
_KubernetesApiClientProtocol.select_header_accept	method	lines 132-132	not-a-behavior-symbol	select_header_accept protocol	candidates: src/mindroom/workers/backends/kubernetes_resources.py:462
_KubernetesApiClientProtocol.call_api	method	lines 134-151	not-a-behavior-symbol	kubernetes api_client call_api merge patch	candidates: src/mindroom/workers/backends/kubernetes_resources.py:462
_CoreApiProtocol	class	lines 154-171	not-a-behavior-symbol	core api protocol service secret pod methods	candidates: src/mindroom/workers/backends/kubernetes_resources.py:324
_CoreApiProtocol.read_namespaced_service	method	lines 157-157	not-a-behavior-symbol	kubernetes read_namespaced_service	candidates: src/mindroom/workers/backends/kubernetes_resources.py:352
_CoreApiProtocol.create_namespaced_service	method	lines 159-159	not-a-behavior-symbol	kubernetes create_namespaced_service	candidates: src/mindroom/workers/backends/kubernetes_resources.py:352
_CoreApiProtocol.patch_namespaced_service	method	lines 161-161	not-a-behavior-symbol	kubernetes patch_namespaced_service	candidates: src/mindroom/workers/backends/kubernetes_resources.py:352
_CoreApiProtocol.delete_namespaced_service	method	lines 163-163	not-a-behavior-symbol	kubernetes delete_namespaced_service	candidates: src/mindroom/workers/backends/kubernetes_resources.py:444
_CoreApiProtocol.read_namespaced_secret	method	lines 165-165	not-a-behavior-symbol	kubernetes read_namespaced_secret	candidates: src/mindroom/workers/backends/kubernetes_resources.py:362
_CoreApiProtocol.create_namespaced_secret	method	lines 167-167	not-a-behavior-symbol	kubernetes create_namespaced_secret	candidates: src/mindroom/workers/backends/kubernetes_resources.py:362
_CoreApiProtocol.delete_namespaced_secret	method	lines 169-169	not-a-behavior-symbol	kubernetes delete_namespaced_secret	candidates: src/mindroom/workers/backends/kubernetes_resources.py:448
_CoreApiProtocol.read_namespaced_pod	method	lines 171-171	not-a-behavior-symbol	kubernetes read_namespaced_pod	candidates: src/mindroom/workers/backends/kubernetes_resources.py:932
worker_id_for_key	function	lines 174-182	related-only	worker id sha256 dns name worker_dir_name	candidates: src/mindroom/tool_system/worker_routing.py:522, src/mindroom/workers/backends/kubernetes.py:617
service_host	function	lines 185-187	none-found	cluster local service host svc.cluster.local sandbox runner endpoint	candidates: src/mindroom/workers/backends/kubernetes.py:645
worker_auth_token	function	lines 190-196	none-found	hmac worker token sandbox proxy token runner auth	candidates: src/mindroom/tool_system/sandbox_proxy.py:463, src/mindroom/api/sandbox_runner.py:128
worker_auth_token_hash	function	lines 199-204	none-found	worker token hash runner token hash sha256	candidates: src/mindroom/workers/backends/kubernetes_resources.py:679
_secret_data_value	function	lines 207-208	related-only	base64 secret data kubernetes secretData b64encode ascii	candidates: src/mindroom/knowledge/manager.py:457, src/mindroom/tool_system/sandbox_proxy.py:663, src/mindroom/api/sandbox_runner.py:1161
parse_annotation_float	function	lines 211-219	none-found	parse annotation float default ValueError	candidates: src/mindroom/workers/backends/kubernetes.py:641, src/mindroom/workers/backends/kubernetes.py:679
parse_annotation_int	function	lines 222-230	none-found	parse annotation int default ValueError	candidates: src/mindroom/workers/backends/kubernetes.py:657, src/mindroom/workers/backends/kubernetes.py:573
metadata_annotations	function	lines 233-259	none-found	worker lifecycle annotations created last_used startup_count failure_count	candidates: src/mindroom/workers/backends/kubernetes.py:372, src/mindroom/workers/backends/kubernetes.py:414
_template_hash	function	lines 262-265	related-only	stable json hash sort_keys separators sha256	candidates: src/mindroom/constants.py:464, src/mindroom/workers/backends/kubernetes_config.py:206, src/mindroom/workers/runtime.py:33
_labels	function	lines 268-276	related-only	kubernetes worker labels selector extra_labels managed-by	candidates: src/mindroom/workers/backends/kubernetes_config.py:207, src/mindroom/workers/backends/kubernetes_resources.py:279
_list_selector	function	lines 279-286	related-only	kubernetes label selector sorted key=value extra_labels	candidates: src/mindroom/workers/backends/kubernetes_resources.py:268
KubernetesResourceManager	class	lines 289-1031	related-only	kubernetes resource manager manifests runtime paths visible roots	candidates: src/mindroom/workers/backends/kubernetes.py:242, src/mindroom/api/sandbox_worker_prep.py:212, src/mindroom/api/sandbox_runner.py:108
KubernetesResourceManager.__init__	method	lines 292-315	none-found	resource manager init cached clients owner reference node cache	candidates: src/mindroom/workers/backends/kubernetes.py:242
KubernetesResourceManager._apps	method	lines 318-321	none-found	lazy apps api load clients property	candidates: src/mindroom/workers/backends/kubernetes_resources.py:582
KubernetesResourceManager._core	method	lines 324-327	none-found	lazy core api load clients property	candidates: src/mindroom/workers/backends/kubernetes_resources.py:582
KubernetesResourceManager._api_exception	method	lines 330-333	none-found	lazy api exception load clients property	candidates: src/mindroom/workers/backends/kubernetes_resources.py:582
KubernetesResourceManager.list_deployments	method	lines 335-341	none-found	list deployments managed selector items	candidates: src/mindroom/workers/backends/kubernetes.py:524
KubernetesResourceManager.read_deployment	method	lines 343-350	related-only	read deployment return none on 404	candidates: src/mindroom/workers/backends/kubernetes_resources.py:575, src/mindroom/workers/backends/kubernetes_resources.py:552
KubernetesResourceManager.apply_service	method	lines 352-360	related-only	create patch service apply object	candidates: src/mindroom/workers/backends/kubernetes_resources.py:552
KubernetesResourceManager.apply_auth_secret	method	lines 362-384	related-only	create patch secret 404 409 merge patch	candidates: src/mindroom/workers/backends/kubernetes_resources.py:552, src/mindroom/workers/backends/kubernetes_resources.py:462
KubernetesResourceManager.apply_deployment	method	lines 386-420	related-only	create patch deployment recreate template hash	candidates: src/mindroom/workers/backends/kubernetes_resources.py:352, src/mindroom/workers/backends/kubernetes_resources.py:486
KubernetesResourceManager.patch_deployment	method	lines 422-438	none-found	merge deployment annotations patch replicas	candidates: src/mindroom/workers/backends/kubernetes.py:487, src/mindroom/workers/backends/kubernetes.py:524
KubernetesResourceManager.delete_deployment	method	lines 440-442	related-only	delete ignoring 404 deployment	candidates: src/mindroom/workers/backends/kubernetes_resources.py:575
KubernetesResourceManager.delete_service	method	lines 444-446	related-only	delete ignoring 404 service	candidates: src/mindroom/workers/backends/kubernetes_resources.py:575
KubernetesResourceManager.delete_secret	method	lines 448-460	related-only	delete secret or remove shared secret key ignoring 404	candidates: src/mindroom/workers/backends/kubernetes_resources.py:575, src/mindroom/workers/backends/kubernetes_resources.py:462
KubernetesResourceManager._patch_secret_merge	method	lines 462-484	none-found	kubernetes secret merge patch call_api application merge-patch	candidates: none
KubernetesResourceManager._recreate_deployment	method	lines 486-511	related-only	delete wait create retry conflict deadline sleep	candidates: src/mindroom/workers/backends/kubernetes_resources.py:513, src/mindroom/workers/backends/kubernetes_resources.py:527
KubernetesResourceManager._wait_for_deployment_absent	method	lines 513-525	related-only	poll until absent timeout sleep	candidates: src/mindroom/workers/backends/kubernetes_resources.py:527, src/mindroom/cli/local_stack.py:341
KubernetesResourceManager.wait_for_ready	method	lines 527-550	related-only	poll until ready timeout sleep on tick	candidates: src/mindroom/workers/backends/kubernetes_resources.py:513, src/mindroom/workers/backends/kubernetes.py:93
KubernetesResourceManager._apply_object	method	lines 552-573	related-only	kubernetes read create patch 404 409 upsert	candidates: src/mindroom/workers/backends/kubernetes_resources.py:352, src/mindroom/workers/backends/kubernetes_resources.py:362
KubernetesResourceManager._delete_object	method	lines 575-580	related-only	kubernetes delete ignore 404	candidates: src/mindroom/workers/backends/kubernetes_resources.py:440, src/mindroom/workers/backends/kubernetes_resources.py:444
KubernetesResourceManager._load_clients	method	lines 582-600	none-found	import kubernetes load incluster config kube config	candidates: none
KubernetesResourceManager._service_manifest	method	lines 602-626	related-only	kubernetes manifest metadata labels ownerReferences service ports	candidates: src/mindroom/workers/backends/kubernetes_resources.py:628, src/mindroom/workers/backends/kubernetes_resources.py:660
KubernetesResourceManager._auth_secret_manifest	method	lines 628-645	related-only	kubernetes secret manifest metadata labels ownerReferences token	candidates: src/mindroom/workers/backends/kubernetes_resources.py:602, src/mindroom/workers/backends/kubernetes_resources.py:647
KubernetesResourceManager._auth_secret_patch	method	lines 647-658	related-only	kubernetes secret patch metadata labels ownerReferences data	candidates: src/mindroom/workers/backends/kubernetes_resources.py:628
KubernetesResourceManager._deployment_manifest	method	lines 660-769	related-only	kubernetes deployment manifest metadata labels ownerReferences env volumes probes	candidates: src/mindroom/workers/backends/kubernetes_resources.py:602, src/mindroom/workers/backends/kubernetes_resources.py:628
KubernetesResourceManager._worker_env	method	lines 771-809	duplicate-found	dedicated worker env sandbox runner mode storage root telemetry	candidates: src/mindroom/workers/backends/kubernetes_resources.py:853, src/mindroom/api/sandbox_runner.py:108, src/mindroom/api/sandbox_exec.py:124
KubernetesResourceManager._worker_token_env	method	lines 811-820	none-found	kubernetes env secretKeyRef sandbox proxy token	candidates: none
KubernetesResourceManager._worker_auth_token	method	lines 822-827	related-only	require worker auth token error	candidates: src/mindroom/workers/backends/kubernetes_resources.py:190, src/mindroom/tool_system/sandbox_proxy.py:463
KubernetesResourceManager._write_startup_manifest	method	lines 829-851	related-only	write startup manifest and sha256	candidates: src/mindroom/constants.py:464, src/mindroom/constants.py:484
KubernetesResourceManager._worker_runtime_paths	method	lines 853-892	duplicate-found	dedicated worker runtime paths sandbox env telemetry isolated runtime	candidates: src/mindroom/workers/backends/kubernetes_resources.py:771, src/mindroom/api/sandbox_runner.py:108, src/mindroom/api/sandbox_exec.py:124, src/mindroom/constants.py:729
KubernetesResourceManager._volume_mounts	method	lines 894-914	related-only	kubernetes volume mounts config map storage mounts	candidates: src/mindroom/workers/backends/kubernetes_resources.py:916, src/mindroom/workers/backends/kubernetes_resources.py:984
KubernetesResourceManager._volumes	method	lines 916-930	related-only	kubernetes volumes config map pvc	candidates: src/mindroom/workers/backends/kubernetes_resources.py:894
KubernetesResourceManager._worker_node_name_or_none	method	lines 932-952	none-found	colocate with control plane node read pod HOSTNAME cache	candidates: none
KubernetesResourceManager._owner_reference_or_none	method	lines 954-982	related-only	ownerReferences deployment uid cache	candidates: src/mindroom/workers/backends/kubernetes_resources.py:602, src/mindroom/workers/backends/kubernetes_resources.py:628, src/mindroom/workers/backends/kubernetes_resources.py:660
KubernetesResourceManager._scoped_storage_mounts	method	lines 984-1031	duplicate-found	visible state roots private agent validation mount path duplicates	candidates: src/mindroom/tool_system/worker_routing.py:597, src/mindroom/api/sandbox_worker_prep.py:212, src/mindroom/api/sandbox_worker_prep.py:245, src/mindroom/api/sandbox_exec.py:108
```

## Findings

### 1. Dedicated worker runtime environment construction is duplicated

`KubernetesResourceManager._worker_env` builds the pod container `env` list from dedicated runner settings, storage paths, shared credentials path, dedicated worker key/root, extra env, and vendor telemetry values at `src/mindroom/workers/backends/kubernetes_resources.py:771`.
`KubernetesResourceManager._worker_runtime_paths` independently builds a `RuntimePaths.process_env` mapping with the same runner mode, execution mode, worker port, storage root, shared credentials path, dedicated worker key/root, extra env, and vendor telemetry values at `src/mindroom/workers/backends/kubernetes_resources.py:853`.
Runner bootstrap and execution code then consumes or reconstructs overlapping runtime state in `src/mindroom/api/sandbox_runner.py:108` and `src/mindroom/api/sandbox_exec.py:124`.

This is functional duplication because the pod environment and startup manifest runtime environment are two representations of the same dedicated worker runtime contract.
Differences to preserve: pod env needs Kubernetes `valueFrom` for the token secret and includes container-specific `VIRTUAL_ENV`, `PATH`, and `HOME`; startup runtime paths need `MappingProxyType`, config path resolution, `GOOGLE_APPLICATION_CREDENTIALS` removal, and the Kubernetes storage subpath prefix for reverse shared-root recovery.

### 2. Scoped storage/private-agent visibility validation is duplicated

`KubernetesResourceManager._scoped_storage_mounts` validates `user_agent` workers require explicit `private_agent_names`, computes visible state roots for mounted and local storage roots, creates local directories, and rejects duplicate mount paths at `src/mindroom/workers/backends/kubernetes_resources.py:984`.
`src/mindroom/api/sandbox_worker_prep.py:245` has the same explicit private-agent requirement for user-agent workers, with a different exception type.
`src/mindroom/api/sandbox_worker_prep.py:212` and `src/mindroom/tool_system/worker_routing.py:597` use the same visible-root contract for request base-dir validation.
`src/mindroom/api/sandbox_exec.py:108` reverses the dedicated worker storage path from the same storage subpath and `worker_dir_name` relationship.

This is functional duplication around the same worker-visible state-root policy.
Differences to preserve: Kubernetes needs both mounted and local path projections plus `subPath` strings; request preparation needs allowed-root validation and `ValueError`/request error handling.

### 3. Worker identifier derivation partially duplicates worker directory derivation

`worker_id_for_key` creates a Kubernetes DNS-safe resource name from a normalized prefix and a 24-character SHA-256 digest at `src/mindroom/workers/backends/kubernetes_resources.py:174`.
`worker_dir_name` creates a filesystem-safe worker directory from a normalized worker-key prefix and a 16-character SHA-256 digest at `src/mindroom/tool_system/worker_routing.py:522`.
`KubernetesWorkerBackend._state_subpath` combines the configured storage prefix and `worker_dir_name` at `src/mindroom/workers/backends/kubernetes.py:617`, while Kubernetes resource manifests use `worker_id_for_key`.

This is related duplication rather than a direct duplicate because Kubernetes names and filesystem paths have different normalization constraints and digest lengths.
The common behavior is stable worker-key-derived identity with prefix fallback and digest suffix.
Differences to preserve: Kubernetes resource names must fit the 63-character DNS label limit; worker directory names retain more worker-key context and are not Kubernetes resource names.

### 4. Repeated Kubernetes metadata/manifest fragments exist but are localized

Service, Secret, Secret patch, and Deployment manifests all repeat metadata assembly: name/namespace, `_labels`, optional `ownerReferences`, and resource-specific payloads at `src/mindroom/workers/backends/kubernetes_resources.py:602`, `src/mindroom/workers/backends/kubernetes_resources.py:628`, `src/mindroom/workers/backends/kubernetes_resources.py:647`, and `src/mindroom/workers/backends/kubernetes_resources.py:660`.

This duplication is real but local to one module and currently readable.
A helper for base metadata could reduce a few lines but would also hide resource-specific shape, so this is not a high-priority refactor.

## Proposed Generalization

1. Add a small internal helper in `kubernetes_resources.py`, for example `_dedicated_worker_env_values(...) -> dict[str, str]`, that returns the shared scalar environment values used by both `_worker_env` and `_worker_runtime_paths`.
2. Keep token `valueFrom`, `VIRTUAL_ENV`, `PATH`, and `HOME` in `_worker_env`, because those are container-manifest-specific.
3. Keep `RuntimePaths`, `MappingProxyType`, config-map path selection, and env filtering in `_worker_runtime_paths`, but source shared dedicated-worker env keys from the helper.
4. Consider moving the explicit user-agent private visibility check to a tiny shared function in `worker_routing.py` only if more call sites appear; today the different exception types make a direct helper less valuable.
5. Do not generalize `worker_id_for_key` with `worker_dir_name` unless a third worker-key identity format appears; the constraints differ enough that separate functions are clearer.

## Risk/tests

The highest-risk duplication is the dedicated worker environment contract.
If refactored, tests should assert `_worker_env` and `_worker_runtime_paths` still agree on runner mode, execution mode, worker port, storage root, shared credentials path, dedicated worker key/root, extra env precedence, and vendor telemetry overrides.

Scoped storage changes would need tests for shared, user, user-agent with private agent names, user-agent without private agent names, invalid worker keys, local directory creation, and duplicate Kubernetes mount path detection.

Worker identifier changes would need tests covering empty prefixes, long prefixes, uppercase prefixes, trailing hyphens, 63-character Kubernetes name length, and stable state subpaths based on `worker_dir_name`.
