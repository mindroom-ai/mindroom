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

{{- define "mindroom-runtime.workerConfigMapName" -}}
{{- if eq (include "mindroom-runtime.configSource" .) "file" -}}
{{- else -}}
{{- default (include "mindroom-runtime.configMapName" .) .Values.workers.kubernetes.configMapName -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.workerConfigKey" -}}
{{- if eq (include "mindroom-runtime.configSource" .) "file" -}}
{{- else -}}
{{- default .Values.config.key .Values.workers.kubernetes.configKey -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.workerConfigPath" -}}
{{- if eq (include "mindroom-runtime.configSource" .) "file" -}}
{{- include "mindroom-runtime.configPath" . -}}
{{- else -}}
{{- default .Values.config.mountPath .Values.workers.kubernetes.configPath -}}
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
{{- $noProxy := join "," . -}}
{{- $_ := set $extraEnv "NO_PROXY" $noProxy -}}
{{- $_ := set $extraEnv "no_proxy" $noProxy -}}
{{- end -}}
{{- end -}}
{{- range $key, $value := .Values.workers.kubernetes.extraEnv -}}
{{- $_ := set $extraEnv $key $value -}}
{{- end -}}
{{- if $extraEnv -}}
{{- toJson $extraEnv -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-runtime.eventCacheNamespace" -}}
{{- default .Release.Namespace .Values.eventCache.namespace -}}
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
cache:
  backend: {{ .Values.eventCache.backend | quote }}
{{- if eq .Values.eventCache.backend "postgres" }}
  database_url_env: {{ .Values.eventCache.databaseUrlEnv | quote }}
  namespace: {{ include "mindroom-runtime.eventCacheNamespace" . | quote }}
{{- else if .Values.eventCache.sqlite.dbPath }}
  db_path: {{ .Values.eventCache.sqlite.dbPath | quote }}
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

{{- define "mindroom-runtime.agentVaultBootstrapLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "mindroom-runtime.agentVaultBootstrapName" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: agent-vault-bootstrap
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
