{{/*
Expand the chart name.
*/}}
{{- define "mindroom-tuwunel.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "mindroom-tuwunel.fullname" -}}
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

{{- define "mindroom-tuwunel.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "mindroom-tuwunel.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}

{{- define "mindroom-tuwunel.selectorLabels" -}}
{{- if .Values.selectorLabels -}}
{{- toYaml .Values.selectorLabels -}}
{{- else -}}
app.kubernetes.io/name: {{ include "mindroom-tuwunel.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: homeserver
{{- end -}}
{{- end -}}

{{- define "mindroom-tuwunel.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if .Values.image.digest -}}
{{- printf "%s:%s@%s" .Values.image.repository $tag .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-tuwunel.configMapName" -}}
{{- if .Values.config.existingConfigMap -}}
{{- .Values.config.existingConfigMap -}}
{{- else -}}
{{- printf "%s-config" (include "mindroom-tuwunel.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-tuwunel.storageClaimName" -}}
{{- if .Values.storage.existingClaim -}}
{{- .Values.storage.existingClaim -}}
{{- else -}}
{{- printf "%s-data" (include "mindroom-tuwunel.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-tuwunel.storageVolumeName" -}}
{{- default "data" .Values.storage.volumeName -}}
{{- end -}}

{{- define "mindroom-tuwunel.clientBaseUrl" -}}
{{- default (printf "https://%s" .Values.tuwunel.serverName) .Values.tuwunel.clientBaseUrl | trimSuffix "/" -}}
{{- end -}}

{{- define "mindroom-tuwunel.wellKnownClient" -}}
{{- default (include "mindroom-tuwunel.clientBaseUrl" .) .Values.tuwunel.wellKnown.client -}}
{{- end -}}

{{- define "mindroom-tuwunel.wellKnownServer" -}}
{{- default (printf "%s:443" .Values.tuwunel.serverName) .Values.tuwunel.wellKnown.server -}}
{{- end -}}

{{- define "mindroom-tuwunel.oidcCallbackUrl" -}}
{{- default (printf "%s/_matrix/client/unstable/login/sso/callback/%s" (include "mindroom-tuwunel.clientBaseUrl" .) .Values.tuwunel.oidc.clientId) .Values.tuwunel.oidc.callbackUrl -}}
{{- end -}}

{{- define "mindroom-tuwunel.registrationTokenDir" -}}/etc/tuwunel/secrets/registration-token{{- end -}}

{{- define "mindroom-tuwunel.registrationTokenFile" -}}
{{- printf "%s/%s" (include "mindroom-tuwunel.registrationTokenDir" .) .Values.tuwunel.registrationToken.key -}}
{{- end -}}

{{- define "mindroom-tuwunel.oidcClientSecretDir" -}}/etc/tuwunel/secrets/oidc{{- end -}}

{{- define "mindroom-tuwunel.oidcClientSecretFile" -}}
{{- printf "%s/%s" (include "mindroom-tuwunel.oidcClientSecretDir" .) .Values.tuwunel.oidc.clientSecret.key -}}
{{- end -}}

{{/*
Rendered tuwunel.toml.
Secret-bearing options reference files mounted from existing Secrets, so no secret material lands in the ConfigMap.
*/}}
{{- define "mindroom-tuwunel.config" -}}
[global]
server_name = {{ .Values.tuwunel.serverName | quote }}
address = ["0.0.0.0"]
port = {{ .Values.tuwunel.port }}
database_path = {{ .Values.storage.mountPath | quote }}
log = {{ .Values.tuwunel.logLevel | quote }}
{{- if .Values.tuwunel.compactEdits }}
mindroom_compact_edits_enabled = true
{{- end }}
{{- if .Values.tuwunel.registrationToken.existingSecret }}
allow_registration = true
registration_token_file = {{ include "mindroom-tuwunel.registrationTokenFile" . | quote }}
{{- end }}
{{- with .Values.tuwunel.extraConfig }}
{{ . }}
{{- end }}

[global.well_known]
client = {{ include "mindroom-tuwunel.wellKnownClient" . | quote }}
server = {{ include "mindroom-tuwunel.wellKnownServer" . | quote }}
{{- if .Values.tuwunel.oidc.enabled }}

[[global.identity_provider]]
brand = {{ .Values.tuwunel.oidc.brand | quote }}
{{- with .Values.tuwunel.oidc.name }}
name = {{ . | quote }}
{{- end }}
client_id = {{ .Values.tuwunel.oidc.clientId | quote }}
client_secret_file = {{ include "mindroom-tuwunel.oidcClientSecretFile" . | quote }}
{{- with .Values.tuwunel.oidc.issuer }}
issuer_url = {{ . | quote }}
{{- end }}
callback_url = {{ include "mindroom-tuwunel.oidcCallbackUrl" . | quote }}
{{- with .Values.tuwunel.oidc.scope }}
scope = {{ toJson . }}
{{- end }}
{{- with .Values.tuwunel.oidc.extraConfig }}
{{ . }}
{{- end }}
{{- end }}
{{- end -}}
