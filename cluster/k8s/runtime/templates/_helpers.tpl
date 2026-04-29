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
{{- default (include "mindroom-runtime.configMapName" .) .Values.workers.kubernetes.configMapName -}}
{{- end -}}

{{- define "mindroom-runtime.workerConfigKey" -}}
{{- default .Values.config.key .Values.workers.kubernetes.configKey -}}
{{- end -}}

{{- define "mindroom-runtime.workerConfigPath" -}}
{{- default .Values.config.mountPath .Values.workers.kubernetes.configPath -}}
{{- end -}}

{{- define "mindroom-runtime.workerNamespace" -}}
{{- default .Release.Namespace .Values.workers.kubernetes.namespace -}}
{{- end -}}

{{- define "mindroom-runtime.workerNetworkPolicyName" -}}
{{- default (printf "%s-workers" (include "mindroom-runtime.fullname" .)) .Values.workers.kubernetes.networkPolicy.name -}}
{{- end -}}
