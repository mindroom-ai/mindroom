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

{{- define "mindroom-tuwunel.appserviceDir" -}}/etc/tuwunel/appservices{{- end -}}

{{- define "mindroom-tuwunel.oidcClientSecretFile" -}}
{{- printf "%s/%s" (include "mindroom-tuwunel.oidcClientSecretDir" .) .Values.tuwunel.oidc.clientSecret.key -}}
{{- end -}}

{{/*
TOML key, quoted unless it is a bare key.
*/}}
{{- define "mindroom-tuwunel.tomlKey" -}}
{{- if regexMatch "^[A-Za-z0-9_-]+$" . -}}
{{- . -}}
{{- else -}}
{{- toJson . -}}
{{- end -}}
{{- end -}}

{{/*
TOML value for a tuwunel.settings entry: strings are rendered with tpl, and lists may nest but not hold tables.
JSON encodes these scalars as valid TOML, including integral numbers as TOML integers.
Arguments: list <root context> <values path> <value>.
*/}}
{{- define "mindroom-tuwunel.tomlValue" -}}
{{- $root := index . 0 -}}
{{- $path := index . 1 -}}
{{- $value := index . 2 -}}
{{- if kindIs "string" $value -}}
{{- tpl $value $root | toJson -}}
{{- else if kindIs "slice" $value -}}
{{- $items := list -}}
{{- range $index, $item := $value -}}
{{- if or (kindIs "map" $item) (kindIs "invalid" $item) -}}
{{- fail (printf "%s[%d] must be a string, number, boolean, or list; use tuwunel.extraConfig for arrays of tables" $path $index) -}}
{{- end -}}
{{- $items = append $items (include "mindroom-tuwunel.tomlValue" (list $root (printf "%s[%d]" $path $index) $item)) -}}
{{- end -}}
{{- printf "[%s]" (join ", " $items) -}}
{{- else -}}
{{- toJson $value -}}
{{- end -}}
{{- end -}}

{{/*
The non-table entries of a tuwunel.settings map as TOML key/value lines; null entries are omitted.
Arguments: list <root context> <values path> <map>.
*/}}
{{- define "mindroom-tuwunel.settingsKeys" -}}
{{- $root := index . 0 -}}
{{- $path := index . 1 -}}
{{- range $key, $value := index . 2 }}
{{- if not (or (kindIs "map" $value) (kindIs "invalid" $value)) }}
{{ include "mindroom-tuwunel.tomlKey" $key }} = {{ include "mindroom-tuwunel.tomlValue" (list $root (printf "%s.%s" $path $key) $value) }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
The map entries of a tuwunel.settings map as TOML tables below table, recursively.
Arguments: list <root context> <values path> <table> <map>.
*/}}
{{- define "mindroom-tuwunel.settingsTables" -}}
{{- $root := index . 0 -}}
{{- $path := index . 1 -}}
{{- $table := index . 2 -}}
{{- range $key, $value := index . 3 }}
{{- if kindIs "map" $value }}
{{- $childPath := printf "%s.%s" $path $key }}
{{- $childTable := printf "%s.%s" $table (include "mindroom-tuwunel.tomlKey" $key) }}

[{{ $childTable }}]
{{- include "mindroom-tuwunel.settingsKeys" (list $root $childPath $value) }}
{{- include "mindroom-tuwunel.settingsTables" (list $root $childPath $childTable $value) }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Rendered tuwunel.toml.
Secret-bearing options reference files mounted from existing Secrets, so no secret material lands in the ConfigMap.
*/}}
{{- define "mindroom-tuwunel.config" -}}
[global]
server_name = {{ .Values.tuwunel.serverName | quote }}
address = {{ toJson .Values.tuwunel.listenAddresses }}
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
{{- if .Values.tuwunel.appserviceRegistration.existingSecret }}
appservice_dir = {{ include "mindroom-tuwunel.appserviceDir" . | quote }}
{{- end }}
{{- include "mindroom-tuwunel.settingsKeys" (list $ "tuwunel.settings" .Values.tuwunel.settings) }}
{{- with .Values.tuwunel.extraConfig }}
{{ . }}
{{- end }}
{{- include "mindroom-tuwunel.settingsTables" (list $ "tuwunel.settings" "global" .Values.tuwunel.settings) }}

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
