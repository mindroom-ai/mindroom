{{/*
Expand the chart name.
*/}}
{{- define "mindroom-matrixrtc.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
Component suffixes are appended, so leave room for them under the 63 character limit.
*/}}
{{- define "mindroom-matrixrtc.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 45 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 45 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 45 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-matrixrtc.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "mindroom-matrixrtc.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}

{{/*
Selector labels for one component; call with (dict "root" $ "component" "livekit").
*/}}
{{- define "mindroom-matrixrtc.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mindroom-matrixrtc.name" .root | quote }}
app.kubernetes.io/instance: {{ .root.Release.Name | quote }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Container image reference; call with (dict "root" $ "image" .Values.<component>.image).
*/}}
{{- define "mindroom-matrixrtc.image" -}}
{{- $tag := .image.tag | default .root.Chart.AppVersion -}}
{{- if .image.digest -}}
{{- printf "%s:%s@%s" .image.repository $tag .image.digest -}}
{{- else -}}
{{- printf "%s:%s" .image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-matrixrtc.livekitName" -}}
{{- printf "%s-livekit" (include "mindroom-matrixrtc.fullname" .) -}}
{{- end -}}

{{- define "mindroom-matrixrtc.mediaServiceName" -}}
{{- printf "%s-livekit-media" (include "mindroom-matrixrtc.fullname" .) -}}
{{- end -}}

{{- define "mindroom-matrixrtc.authName" -}}
{{- printf "%s-auth" (include "mindroom-matrixrtc.fullname" .) -}}
{{- end -}}

{{/*
Rendered LiveKit config.yaml.
API keys stay out of the ConfigMap; LiveKit reads them from the LIVEKIT_KEYS environment variable.
*/}}
{{- define "mindroom-matrixrtc.livekitConfig" -}}
{{- $media := .Values.livekit.media -}}
{{- $config := dict
  "port" (int .Values.livekit.port)
  "logging" (dict "level" .Values.livekit.logLevel)
  "rtc" (dict
    "tcp_port" (int $media.ports.tcp)
    "udp_port" (int $media.ports.udp)
    "node_ip" $media.loadBalancerIP
    "use_external_ip" false)
  "room" (dict "auto_create" false)
-}}
{{- toYaml (mustMergeOverwrite $config (deepCopy .Values.livekit.extraConfig)) -}}
{{- end -}}

{{/*
Ingress peers allowed to reach the public HTTP endpoints of both components.
*/}}
{{- define "mindroom-matrixrtc.clientPeers" -}}
{{- with .Values.networkPolicy.clientPodSelector }}
- podSelector:
    {{- toYaml . | nindent 4 }}
{{- end }}
{{- with .Values.networkPolicy.extraFrom }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Pod spec fields shared by both components.
Service links stay disabled because LiveKit parses LIVEKIT_* variables such as LIVEKIT_PORT as config flags.
*/}}
{{- define "mindroom-matrixrtc.podSpecCommon" -}}
automountServiceAccountToken: false
enableServiceLinks: false
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- if .Values.priorityClassName }}
priorityClassName: {{ .Values.priorityClassName | quote }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.podSecurityContext }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
