{{/*
Expand the chart name.
*/}}
{{- define "mindroom-runtime.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "mindroom-runtime.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "mindroom-runtime.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}

{{- define "mindroom-runtime.selectorLabels" -}}
{{- if .Values.selectorLabels -}}
{{- toYaml .Values.selectorLabels -}}
{{- else -}}
app.kubernetes.io/name: {{ include "mindroom-runtime.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: runtime
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if .Values.image.digest -}}
{{- printf "%s:%s@%s" .Values.image.repository $tag .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.workerImage" -}}
{{- $image := .Values.workers.kubernetes.image -}}
{{- if $image.repository -}}
{{- $tag := $image.tag | default .Values.image.tag | default .Chart.AppVersion -}}
{{- $digest := $image.digest | default .Values.image.digest -}}
{{- if $digest -}}
{{- printf "%s:%s@%s" $image.repository $tag $digest -}}
{{- else -}}
{{- printf "%s:%s" $image.repository $tag -}}
{{- end -}}
{{- else -}}
{{- include "mindroom-runtime.image" . -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.configMapName" -}}
{{- if .Values.config.existingConfigMap -}}
{{- .Values.config.existingConfigMap -}}
{{- else -}}
{{- printf "%s-config" (include "mindroom-runtime.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.configSource" -}}
{{- default "configMap" .Values.config.source -}}
{{- end -}}

{{- define "mindroom-runtime.usesConfigMapConfig" -}}
{{- if eq (include "mindroom-runtime.configSource" .) "configMap" -}}true{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.configPath" -}}
{{- if eq (include "mindroom-runtime.configSource" .) "file" -}}
{{- .Values.config.path -}}
{{- else -}}
{{- .Values.config.mountPath -}}
{{- end -}}
{{- end -}}

{{- /*
First path component below storage.mountPath that holds a file-sourced config, or "" when there is none.
The static runner sidecar mounts the agents and private_instances directories, so the config must stay outside them.
*/ -}}
{{- define "mindroom-runtime.fileConfigStorageSubpath" -}}
{{- if eq (include "mindroom-runtime.configSource" .) "file" -}}
{{- $prefix := printf "%s/" (trimSuffix "/" (clean .Values.storage.mountPath)) -}}
{{- $configPath := clean .Values.config.path -}}
{{- if hasPrefix $prefix $configPath -}}
{{- first (splitList "/" (trimPrefix $prefix $configPath)) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.storageClaimName" -}}
{{- if .Values.storage.existingClaim -}}
{{- .Values.storage.existingClaim -}}
{{- else -}}
{{- printf "%s-storage" (include "mindroom-runtime.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.storageVolumeName" -}}
{{- default "storage" .Values.storage.volumeName -}}
{{- end -}}

{{- define "mindroom-runtime.stateStorageClaimName" -}}
{{- if .Values.stateStorage.existingClaim -}}
{{- .Values.stateStorage.existingClaim -}}
{{- else -}}
{{- printf "%s-state" (include "mindroom-runtime.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.stateStorageVolumeName" -}}
{{- default "state-storage" .Values.stateStorage.volumeName -}}
{{- end -}}

{{- /*
Quoted state PVC directories, as seen at /state, that the prepare-state-storage init container creates and chowns.
*/ -}}
{{- define "mindroom-runtime.stateStorageInitDirs" -}}
{{- $dirs := list "/state" -}}
{{- if .Values.stateStorage.encryptionKeys.enabled -}}
{{- $dirs = append $dirs (printf "/state/%s" .Values.stateStorage.encryptionKeys.subPath) -}}
{{- end -}}
{{- if .Values.stateStorage.syncContinuity.enabled -}}
{{- $dirs = append $dirs (printf "/state/%s" .Values.stateStorage.syncContinuity.subPath) -}}
{{- end -}}
{{- range .Values.stateStorage.extraSubPaths -}}
{{- $dirs = append $dirs (printf "/state/%s" (default .name .subPath)) -}}
{{- end -}}
{{- range $index, $dir := $dirs }}{{ if $index }} {{ end }}"{{ $dir }}"{{ end -}}
{{- end -}}

{{- define "mindroom-runtime.sessionStorageClaimName" -}}
{{- default (printf "%s-sessions" (include "mindroom-runtime.fullname" .)) .Values.sessionStorage.existingClaim -}}
{{- end -}}

{{- define "mindroom-runtime.knowledgeStorageClaimName" -}}
{{- default (printf "%s-knowledge" (include "mindroom-runtime.fullname" .)) .Values.knowledgeStorage.existingClaim -}}
{{- end -}}

{{- /*
Shared knowledge-base indexes live at <storage.mountPath>/knowledge_db; KnowledgeManager has no override for it.
*/ -}}
{{- define "mindroom-runtime.knowledgeStorageMountPath" -}}
{{- printf "%s/knowledge_db" (trimSuffix "/" (clean .Values.storage.mountPath)) -}}
{{- end -}}

{{- /*
Chart-managed PersistentVolumeClaim; takes (list $ claimName valuesBlock) where valuesBlock has accessModes, storageClassName, and size.
*/ -}}
{{- define "mindroom-runtime.persistentVolumeClaim" -}}
{{- $root := index . 0 -}}
{{- $values := index . 2 -}}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ index . 1 }}
  labels:
    {{- include "mindroom-runtime.labels" $root | nindent 4 }}
spec:
  accessModes:
    {{- toYaml $values.accessModes | nindent 4 }}
  {{- if $values.storageClassName }}
  storageClassName: {{ $values.storageClassName | quote }}
  {{- end }}
  resources:
    requests:
      storage: {{ $values.size | quote }}
{{- end -}}

{{- define "mindroom-runtime.contentBundleSourcePath" -}}
{{- $bundle := index . 1 -}}
{{- $sourcePath := default "/bundle" $bundle.sourcePath | clean -}}
{{- if eq $sourcePath "/" -}}/{{- else -}}{{ $sourcePath | trimSuffix "/" }}{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.contentBundleTargetPath" -}}
{{- $root := index . 0 -}}
{{- $bundle := index . 1 -}}
{{- $targetPath := default (printf "%s/content-bundles/%s" ($root.Values.storage.mountPath | trimSuffix "/") $bundle.name) $bundle.targetPath | clean -}}
{{- if eq $targetPath "/" -}}/{{- else -}}{{ $targetPath | trimSuffix "/" }}{{- end -}}
{{- end -}}

{{/*
Native bootstrap source as JSON: the explicit config.bootstrapBundlePath and revision,
or the selected content bundle's target path plus subPath, with a revision hashed from its
image digest and the selected image directory. root is the directory the transport replaces.
*/}}
{{- define "mindroom-runtime.bootstrapBundle" -}}
{{- $bootstrap := dict "path" (default "" .Values.config.bootstrapBundlePath) "root" (default "" .Values.config.bootstrapBundlePath) "revision" (default "" .Values.config.bootstrapBundleRevision) -}}
{{- $selected := .Values.config.bootstrapContentBundle.name -}}
{{- if $selected -}}
{{- $bootstrap = dict "path" "" "root" "" "revision" "" -}}
{{- range $bundle := $.Values.contentBundles -}}
{{- if eq (toString $bundle.name) (toString $selected) -}}
{{- $subPath := default "" $.Values.config.bootstrapContentBundle.subPath -}}
{{- $targetPath := include "mindroom-runtime.contentBundleTargetPath" (list $ $bundle) -}}
{{- $imagePath := clean (printf "%s/%s" (include "mindroom-runtime.contentBundleSourcePath" (list $ $bundle)) $subPath) -}}
{{- $digest := trimPrefix "@sha256:" (regexFind "@sha256:[a-f0-9]{64}$" (toString $bundle.image)) -}}
{{- $_ := set $bootstrap "root" $targetPath -}}
{{- $_ := set $bootstrap "path" (clean (printf "%s/%s" $targetPath $subPath)) -}}
{{- $_ := set $bootstrap "revision" (sha256sum (printf "%s:%s" $digest $imagePath)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- toJson $bootstrap -}}
{{- end -}}

{{- define "mindroom-runtime.contentBundleSeedCommand" -}}
{{- $bundle := index . 0 -}}
{{- range $argIndex, $arg := $bundle.seed.command -}}
{{- if $argIndex }} {{ end -}}{{ $arg | quote -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "mindroom-runtime.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.workerServiceAccountName" -}}
{{- if .Values.workers.kubernetes.serviceAccount.create -}}
{{- default (printf "%s-worker" (include "mindroom-runtime.fullname" .)) .Values.workers.kubernetes.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.workers.kubernetes.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.proxyTokenSecretName" -}}
{{- if .Values.workers.sandbox.proxyToken.existingSecret -}}
{{- .Values.workers.sandbox.proxyToken.existingSecret -}}
{{- else if .Values.workers.sandbox.proxyToken.value -}}
{{- printf "%s-sandbox-proxy" (include "mindroom-runtime.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.workerNamespace" -}}
{{- default .Release.Namespace .Values.workers.kubernetes.namespace -}}
{{- end -}}

{{- define "mindroom-runtime.workerAuthSecretName" -}}
{{- if and (eq .Values.workers.backend "kubernetes") (eq (include "mindroom-runtime.workerNamespace" .) .Release.Namespace) -}}
{{- printf "%s-worker-auth" (include "mindroom-runtime.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.networkPolicyName" -}}
{{- default (include "mindroom-runtime.fullname" .) .Values.networkPolicy.name -}}
{{- end -}}

{{- define "mindroom-runtime.workerNetworkPolicyName" -}}
{{- default (printf "%s-workers" (include "mindroom-runtime.fullname" .)) .Values.workers.kubernetes.networkPolicy.name -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressName" -}}
{{- default (printf "%s-egress-proxy" (include "mindroom-runtime.fullname" .) | trunc 63 | trimSuffix "-") .Values.approvedEgress.service.name -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressServiceAccountName" -}}
{{- if .Values.approvedEgress.serviceAccount.create -}}
{{- default (include "mindroom-runtime.approvedEgressName" .) .Values.approvedEgress.serviceAccount.name -}}
{{- else -}}
{{- .Values.approvedEgress.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressConfigMapName" -}}
{{- if .Values.approvedEgress.allowlist.existingConfigMap -}}
{{- .Values.approvedEgress.allowlist.existingConfigMap -}}
{{- else -}}
{{- printf "%s-config" (include "mindroom-runtime.approvedEgressName" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressSquidConfigMapName" -}}
{{- printf "%s-squid-config" (include "mindroom-runtime.approvedEgressName" .) -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressSquidConfigPath" -}}
/etc/squid/mindroom-egress-chain.conf
{{- end -}}

{{- define "mindroom-runtime.approvedEgressSquidConfig" -}}
include /etc/squid/squid.conf

acl egress_has_token req_header Proxy-Authorization .
{{- with .Values.approvedEgress.parentProxy.bypassDomains }}
acl egress_bypass_parent dstdomain -n {{ join " " . }}
{{- end }}
dns_defnames on
cache_peer {{ .Values.approvedEgress.parentProxy.host }} parent {{ .Values.approvedEgress.parentProxy.port }} 0 no-query no-digest login=PASSTHRU
{{- if .Values.approvedEgress.parentProxy.bypassDomains }}
cache_peer_access {{ .Values.approvedEgress.parentProxy.host }} deny egress_bypass_parent
{{- end }}
cache_peer_access {{ .Values.approvedEgress.parentProxy.host }} allow egress_has_token
cache_peer_access {{ .Values.approvedEgress.parentProxy.host }} deny all
nonhierarchical_direct off
{{- if .Values.approvedEgress.parentProxy.bypassDomains }}
always_direct allow egress_bypass_parent
{{- end }}
always_direct allow !egress_has_token
never_direct allow egress_has_token
{{- end -}}

{{- define "mindroom-runtime.approvedEgressClaimName" -}}
{{- if .Values.approvedEgress.persistence.existingClaim -}}
{{- .Values.approvedEgress.persistence.existingClaim -}}
{{- else -}}
{{- printf "%s-data" (include "mindroom-runtime.approvedEgressName" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressTokenSecretName" -}}
{{- if .Values.approvedEgress.token.existingSecret -}}
{{- .Values.approvedEgress.token.existingSecret -}}
{{- else -}}
{{- include "mindroom-runtime.proxyTokenSecretName" . -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressTokenSecretKey" -}}
{{- if .Values.approvedEgress.token.existingSecret -}}
{{- .Values.approvedEgress.token.key -}}
{{- else -}}
{{- .Values.workers.sandbox.proxyToken.key -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressImage" -}}
{{- $image := .Values.approvedEgress.image -}}
{{- if and $image.tag $image.digest -}}
{{- printf "%s:%s@%s" $image.repository $image.tag $image.digest -}}
{{- else if $image.digest -}}
{{- printf "%s@%s" $image.repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $image.repository $image.tag -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressSelectorLabels" -}}
app.kubernetes.io/name: {{ include "mindroom-runtime.approvedEgressName" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: approved-egress-proxy
{{- end -}}

{{- define "mindroom-runtime.approvedEgressLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{ include "mindroom-runtime.approvedEgressSelectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressPodSelector" -}}
matchLabels:
  {{- include "mindroom-runtime.approvedEgressSelectorLabels" . | nindent 2 }}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressNetworkPolicyName" -}}
{{- default (printf "%s-ingress" (include "mindroom-runtime.approvedEgressName" .)) .Values.approvedEgress.networkPolicy.name -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressDbPath" -}}
{{- printf "%s/%s" (.Values.approvedEgress.dataPath | trimSuffix "/") .Values.approvedEgress.dbFileName -}}
{{- end -}}

{{- define "mindroom-runtime.approvedEgressApiUrl" -}}
{{- printf "http://%s.%s.svc.cluster.local:%v" (include "mindroom-runtime.approvedEgressName" .) .Release.Namespace .Values.approvedEgress.service.apiPort -}}
{{- end -}}

{{- define "mindroom-runtime.egressProxyEnabled" -}}
{{- if or .Values.egressProxy.enabled .Values.approvedEgress.enabled -}}true{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.egressProxyNamespace" -}}
{{- if .Values.approvedEgress.enabled -}}
{{- .Release.Namespace -}}
{{- else -}}
{{- default .Release.Namespace .Values.egressProxy.service.namespace -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.egressProxyServiceName" -}}
{{- if .Values.approvedEgress.enabled -}}
{{- include "mindroom-runtime.approvedEgressName" . -}}
{{- else -}}
{{- .Values.egressProxy.service.name -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.egressProxyServicePort" -}}
{{- if .Values.approvedEgress.enabled -}}
{{- .Values.approvedEgress.service.proxyPort -}}
{{- else -}}
{{- .Values.egressProxy.service.port -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.egressProxyPodSelector" -}}
{{- if .Values.approvedEgress.enabled -}}
{{- include "mindroom-runtime.approvedEgressPodSelector" . -}}
{{- else -}}
{{- toYaml .Values.egressProxy.networkPolicy.proxyPodSelector -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.egressProxyUrl" -}}
{{- $namespace := include "mindroom-runtime.egressProxyNamespace" . -}}
{{- $serviceName := include "mindroom-runtime.egressProxyServiceName" . -}}
{{- $servicePort := include "mindroom-runtime.egressProxyServicePort" . -}}
{{- $scheme := .Values.egressProxy.service.scheme -}}
{{- if .Values.approvedEgress.enabled -}}
{{- $scheme = "http" -}}
{{- end -}}
{{- printf "%s://%s.%s.svc.cluster.local:%v" $scheme $serviceName $namespace $servicePort -}}
{{- end -}}

{{- define "mindroom-runtime.agentVaultProxyUrl" -}}
{{- if and .Values.approvedEgress.enabled .Values.approvedEgress.parentProxy.enabled -}}
{{- include "mindroom-runtime.egressProxyUrl" . -}}
{{- else -}}
{{- .Values.workers.kubernetes.agentVault.proxyUrl -}}
{{- end -}}
{{- end -}}

{{/*
Normalize a list value that may also be written as a map keyed by entry.
Helm replaces lists wholesale across values files but merges maps key by key, so the map form lets a later file override or remove one entry.
Map entries render in key order, take nameKey (when set) from their key unless they set it, and drop null entries and null fields.
String map values become {nameKey: key, scalarKey: value} when scalarKey is set.
Arguments: dict "value" <list or map> "path" <values path> "nameKey" <field or ""> "scalarKey" <field or "">.
Returns a JSON array for fromJsonArray.
*/}}
{{- define "mindroom-runtime.keyedList" -}}
{{- $items := list -}}
{{- if kindIs "map" .value -}}
{{- range $key := keys .value | sortAlpha -}}
{{- $item := index $.value $key -}}
{{- if kindIs "map" $item -}}
{{- $entry := dict -}}
{{- with $.nameKey -}}
{{- $_ := set $entry . $key -}}
{{- end -}}
{{- range $field, $fieldValue := $item -}}
{{- if not (kindIs "invalid" $fieldValue) -}}
{{- $_ := set $entry $field $fieldValue -}}
{{- end -}}
{{- end -}}
{{- $items = append $items $entry -}}
{{- else if and $.scalarKey (kindIs "string" $item) -}}
{{- $items = append $items (dict $.nameKey $key $.scalarKey $item) -}}
{{- else if kindIs "invalid" $item -}}
{{- /* A null entry removes an entry set by an earlier values file. */ -}}
{{- else if $.scalarKey -}}
{{- fail (printf "%s.%s must be a string, a map, or null; quote numbers and booleans" $.path $key) -}}
{{- else -}}
{{- fail (printf "%s.%s must be a map or null" $.path $key) -}}
{{- end -}}
{{- end -}}
{{- else if kindIs "slice" .value -}}
{{- $items = .value -}}
{{- else if not (kindIs "invalid" .value) -}}
{{- fail (printf "%s must be a list or a map" .path) -}}
{{- end -}}
{{- toJson $items -}}
{{- end -}}

{{/*
EnvVar entries from a list or a name-keyed map (see keyedList).
A map value may be a plain string shorthand for {value: ...}.
String values are rendered with tpl, so they can reference values such as {{ .Release.Namespace }}.
Arguments: list <root context> <list or map> <values path>.
*/}}
{{- define "mindroom-runtime.envList" -}}
{{- $root := index . 0 -}}
{{- $env := list -}}
{{- range $entry := include "mindroom-runtime.keyedList" (dict "value" (index . 1) "path" (index . 2) "nameKey" "name" "scalarKey" "value") | fromJsonArray -}}
{{- if kindIs "string" $entry.value -}}
{{- $_ := set $entry "value" (tpl $entry.value $root) -}}
{{- end -}}
{{- $env = append $env $entry -}}
{{- end -}}
{{- toJson $env -}}
{{- end -}}

{{- define "mindroom-runtime.workerExtraEnvJson" -}}
{{- $extraEnv := dict -}}
{{- if and (include "mindroom-runtime.egressProxyEnabled" .) .Values.egressProxy.injectWorkerProxyEnv -}}
{{- $proxyUrl := include "mindroom-runtime.egressProxyUrl" . -}}
{{- $_ := set $extraEnv "HTTP_PROXY" $proxyUrl -}}
{{- $_ := set $extraEnv "HTTPS_PROXY" $proxyUrl -}}
{{- $_ := set $extraEnv "ALL_PROXY" $proxyUrl -}}
{{- $_ := set $extraEnv "http_proxy" $proxyUrl -}}
{{- $_ := set $extraEnv "https_proxy" $proxyUrl -}}
{{- $_ := set $extraEnv "all_proxy" $proxyUrl -}}
{{- with .Values.egressProxy.noProxy -}}
{{- $noProxy := list -}}
{{- range . -}}
{{- $noProxy = append $noProxy (tpl (toString .) $) -}}
{{- end -}}
{{- $_ := set $extraEnv "NO_PROXY" (join "," $noProxy) -}}
{{- $_ := set $extraEnv "no_proxy" (join "," $noProxy) -}}
{{- end -}}
{{- end -}}
{{- range $key, $value := .Values.workers.kubernetes.extraEnv -}}
{{- if kindIs "string" $value -}}
{{- $value = tpl $value $ -}}
{{- end -}}
{{- $_ := set $extraEnv $key $value -}}
{{- end -}}
{{- if $extraEnv -}}
{{- toJson $extraEnv -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.eventCacheDatabaseUrlSecretKey" -}}
{{- default .Values.eventCache.databaseUrlEnv .Values.eventCache.databaseUrl.key -}}
{{- end -}}

{{- define "mindroom-runtime.eventCachePostgresName" -}}
{{- default (printf "%s-event-cache-postgres" (include "mindroom-runtime.fullname" .)) .Values.eventCache.postgres.nameOverride -}}
{{- end -}}

{{- define "mindroom-runtime.eventCachePostgresSecretName" -}}
{{- printf "%s-auth" (include "mindroom-runtime.eventCachePostgresName" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mindroom-runtime.eventCachePostgresPasswordSecretName" -}}
{{- if .Values.eventCache.postgres.auth.existingSecret -}}
{{- .Values.eventCache.postgres.auth.existingSecret -}}
{{- else -}}
{{- include "mindroom-runtime.eventCachePostgresSecretName" . -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.eventCacheDatabaseUrlSecretName" -}}
{{- if .Values.eventCache.databaseUrl.existingSecret -}}
{{- .Values.eventCache.databaseUrl.existingSecret -}}
{{- else if and .Values.eventCache.postgres.create (not .Values.eventCache.postgres.auth.existingSecret) -}}
{{- include "mindroom-runtime.eventCachePostgresSecretName" . -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.eventCachePostgresImage" -}}
{{- printf "%s:%s" .Values.eventCache.postgres.image.repository .Values.eventCache.postgres.image.tag -}}
{{- end -}}

{{- define "mindroom-runtime.eventCachePostgresSelectorLabels" -}}
{{- if .Values.eventCache.postgres.selectorLabels -}}
{{- toYaml .Values.eventCache.postgres.selectorLabels -}}
{{- else -}}
app.kubernetes.io/name: {{ include "mindroom-runtime.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: event-cache-postgres
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.eventCachePostgresVolumeName" -}}
{{- default "data" .Values.eventCache.postgres.persistence.volumeName -}}
{{- end -}}

{{- define "mindroom-runtime.eventCachePostgresNetworkPolicyName" -}}
{{- default (include "mindroom-runtime.eventCachePostgresName" .) .Values.eventCache.postgres.networkPolicy.name -}}
{{- end -}}

{{- define "mindroom-runtime.eventCachePostgresDatabaseUrl" -}}
{{- $root := .root -}}
{{- $password := .password -}}
{{- printf "postgresql://%s:%s@%s:%v/%s" ($root.Values.eventCache.postgres.auth.username | urlquery) ($password | urlquery) (include "mindroom-runtime.eventCachePostgresName" $root) $root.Values.eventCache.postgres.service.port ($root.Values.eventCache.postgres.auth.database | urlquery) -}}
{{- end -}}

{{- define "mindroom-runtime.defaultConfig" -}}
agents: {}
models: {}
event_journal:
  backend: {{ .Values.eventCache.backend | quote }}
{{- if eq .Values.eventCache.backend "postgres" }}
  database_url_env: {{ .Values.eventCache.databaseUrlEnv | quote }}
{{- end }}
{{- end -}}

{{- define "mindroom-runtime.agentVaultServerName" -}}
{{- default "agent-vault" .Values.workers.kubernetes.agentVault.server.name -}}
{{- end -}}

{{- define "mindroom-runtime.agentVaultSelectorLabels" -}}
app.kubernetes.io/name: {{ include "mindroom-runtime.agentVaultServerName" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: agent-vault
{{- end -}}

{{- define "mindroom-runtime.agentVaultLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{ include "mindroom-runtime.agentVaultSelectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}

{{- define "mindroom-runtime.agentVaultServerClaimName" -}}
{{- if .Values.workers.kubernetes.agentVault.server.persistence.existingClaim -}}
{{- .Values.workers.kubernetes.agentVault.server.persistence.existingClaim -}}
{{- else -}}
{{- printf "%s-data" (include "mindroom-runtime.agentVaultServerName" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.agentVaultBootstrapName" -}}
{{- printf "%s-bootstrap" (include "mindroom-runtime.agentVaultServerName" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mindroom-runtime.agentVaultAccessGrantsName" -}}
{{- printf "%s-access-grants" (include "mindroom-runtime.agentVaultServerName" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Agent Vault Job (or grants ConfigMap) name from (list root baseName renderedInputs).
jobNaming=contentHash appends a hash of the rendered inputs, so a plain `kubectl apply`
creates a new object when they change and leaves the existing one alone otherwise.
*/}}
{{- define "mindroom-runtime.agentVaultJobName" -}}
{{- $root := index . 0 -}}
{{- $name := index . 1 -}}
{{- if eq $root.Values.workers.kubernetes.agentVault.jobNaming "contentHash" -}}
{{- printf "%s-%s" ($name | trunc 52 | trimSuffix "-") (index . 2 | sha256sum | trunc 10) -}}
{{- else -}}
{{- $name -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.agentVaultAccessGrantsConfigPath" -}}
/etc/mindroom-agent-vault-access-grants/access-grants.yaml
{{- end -}}

{{- define "mindroom-runtime.agentVaultAccessGrantsTokenPath" -}}
{{- printf "/etc/agent-vault-access-grants/%s" .Values.workers.kubernetes.agentVault.accessGrants.adminTokenSecret.key -}}
{{- end -}}

{{- define "mindroom-runtime.agentVaultBootstrapLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "mindroom-runtime.agentVaultBootstrapName" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: agent-vault-bootstrap
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}

{{- define "mindroom-runtime.agentVaultAccessGrantsLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "mindroom-runtime.agentVaultAccessGrantsName" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: agent-vault-access-grants
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}

{{- define "mindroom-runtime.agentVaultServerImage" -}}
{{- $av := .Values.workers.kubernetes.agentVault -}}
{{- required "workers.kubernetes.agentVault.server.image or cliImage is required when the Agent Vault server is enabled" (default $av.cliImage $av.server.image) -}}
{{- end -}}

{{- define "mindroom-runtime.agentVaultBootstrapImage" -}}
{{- $av := .Values.workers.kubernetes.agentVault -}}
{{- required "workers.kubernetes.agentVault.bootstrap.image or cliImage is required when bootstrap is enabled" (default $av.cliImage $av.bootstrap.image) -}}
{{- end -}}

{{/*
Provider API key env vars the runtime natively syncs into its credential
service at startup. Mirrors PROVIDER_ENV_KEYS in src/mindroom/constants.py.
*/}}
{{- define "mindroom-runtime.providerCredentialEnvMap" -}}
anthropic: ANTHROPIC_API_KEY
azure: AZURE_OPENAI_API_KEY
openai: OPENAI_API_KEY
google: GOOGLE_API_KEY
openrouter: OPENROUTER_API_KEY
deepseek: DEEPSEEK_API_KEY
cerebras: CEREBRAS_API_KEY
groq: GROQ_API_KEY
zai: ZAI_API_KEY
ollama: OLLAMA_HOST
{{- end -}}

{{- define "mindroom-runtime.providerCredentialEnvName" -}}
{{- $entry := index . 1 -}}
{{- $envMap := include "mindroom-runtime.providerCredentialEnvMap" (index . 0) | fromYaml -}}
{{- index $envMap $entry.provider -}}
{{- end -}}
