{{/*
Expand the chart name.
*/}}
{{- define "mindroom-client.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "mindroom-client.fullname" -}}
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

{{- define "mindroom-client.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "mindroom-client.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}

{{- define "mindroom-client.selectorLabels" -}}
{{- if .Values.selectorLabels -}}
{{- toYaml .Values.selectorLabels -}}
{{- else -}}
app.kubernetes.io/name: {{ include "mindroom-client.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: client
{{- end -}}
{{- end -}}

{{- define "mindroom-client.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if .Values.image.digest -}}
{{- printf "%s:%s@%s" .Values.image.repository $tag .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-client.configMapName" -}}
{{- if .Values.config.existingConfigMap -}}
{{- .Values.config.existingConfigMap -}}
{{- else -}}
{{- printf "%s-config" (include "mindroom-client.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mindroom-client.nginxConfigMapName" -}}
{{- if .Values.nginx.existingConfigMap -}}
{{- .Values.nginx.existingConfigMap -}}
{{- else -}}
{{- printf "%s-nginx" (include "mindroom-client.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Normalized base path: leading slash, no trailing slash, "/" for the origin root.
*/}}
{{- define "mindroom-client.basePath" -}}
{{- $path := default "/" .Values.basePath | clean | trimSuffix "/" -}}
{{- default "/" $path -}}
{{- end -}}

{{/*
Path prefix prepended to client file locations; empty at the origin root.
*/}}
{{- define "mindroom-client.pathPrefix" -}}
{{- $base := include "mindroom-client.basePath" . -}}
{{- if ne $base "/" -}}{{- $base -}}{{- end -}}
{{- end -}}

{{- define "mindroom-client.probePath" -}}
{{- printf "%s/config.json" (include "mindroom-client.pathPrefix" .) -}}
{{- end -}}

{{- define "mindroom-client.defaultClientConfig" -}}
{{- $homeserver := default .Values.matrix.homeserverUrl .Values.matrix.defaultServerName -}}
{
  "defaultHomeserver": 0,
  "homeserverList": [{{ $homeserver | quote }}],
  "allowCustomHomeservers": {{ .Values.matrix.allowCustomHomeservers }},
  "hashRouter": {
    "enabled": false,
    "basename": "/"
  }
}
{{- end -}}

{{/*
The nginx server config serving the client under basePath.
The image entrypoint normally writes runtime-config.js into the app directory,
which an unprivileged read-only container forbids, so the chart serves
runtime-config.js straight from nginx and the Deployment bypasses the entrypoint.
*/}}
{{- define "mindroom-client.defaultNginxConf" -}}
{{- $base := include "mindroom-client.basePath" . -}}
{{- $prefix := include "mindroom-client.pathPrefix" . -}}
{{- $runtimeConfig := printf "window.__APP_BASE_PATH__ = \"%s\"; window.__ENABLE_SERVICE_WORKER__ = %t;" $base .Values.serviceWorker.enabled -}}
server {
  listen {{ .Values.nginx.port }};
{{- if .Values.nginx.ipv6 }}
  listen [::]:{{ .Values.nginx.port }};
{{- end }}
  absolute_redirect off;
{{- if ne $base "/" }}

  location = / {
    return 308 {{ $base }}/;
  }

  location = {{ $base }} {
    return 308 {{ $base }}/;
  }
{{- end }}

  # index.html loads /runtime-config.js from the origin root regardless of basePath.
  location = /runtime-config.js {
    default_type application/javascript;
    add_header Cache-Control "no-store, max-age=0" always;
    return 200 '{{ $runtimeConfig }}';
  }

  location = {{ $prefix }}/config.json {
    alias /usr/share/nginx/html/config.json;
    default_type application/json;
    add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;
  }

  # The client service worker, scoped to basePath.
  location = {{ $prefix }}/sw.js {
    alias /usr/share/nginx/html/sw.js;
    default_type application/javascript;
    add_header Cache-Control "no-store, max-age=0" always;
  }
{{- if .Values.rootServiceWorkerCleanup.enabled }}

  # A Matrix client that previously controlled the origin root can leave a
  # root-scoped service worker that keeps serving the old app for every path on
  # this origin. Serving this cleanup worker at the old /sw.js URL makes the
  # stale worker replace itself with one that purges caches and unregisters.
  location = /sw.js {
    alias /usr/share/nginx/html/root-cleanup-sw.js;
    default_type application/javascript;
    add_header Cache-Control "no-store, max-age=0" always;
  }
{{- end }}
{{- if ne $base "/" }}

  location = {{ $prefix }}/manifest.json {
    alias /usr/share/nginx/html/manifest.json;
    add_header Cache-Control "no-store, max-age=0" always;
  }

  location = {{ $prefix }}/pdf.worker.min.js {
    alias /usr/share/nginx/html/pdf.worker.min.js;
  }
{{- end }}

  # Hashed build assets are referenced relative to the current route, so strip
  # any route prefix before the assets/ or public/ segment.
  location ~ ^/(?:.+/)?(assets|public)/(.+)$ {
    root /usr/share/nginx/html;
    try_files /$1/$2 =404;
    add_header Cache-Control "public, max-age=31536000, immutable" always;
  }
{{- if eq $base "/" }}

  location / {
    root /usr/share/nginx/html;
    add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;
    try_files $uri $uri/ /index.html;
  }
{{- else }}

  # Everything else under basePath is an app route served by index.html.
  location {{ $base }}/ {
    root /usr/share/nginx/html;
    add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;
    try_files /index.html =404;
  }
{{- end }}
}
{{- end -}}

{{/*
Service worker served at the origin-root /sw.js to replace a stale root-scoped
worker: it purges client caches, releases controlled pages into basePath, and
unregisters itself.
*/}}
{{- define "mindroom-client.rootCleanupServiceWorker" -}}
{{- $target := printf "%s/" (include "mindroom-client.pathPrefix" .) -}}
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      if ("caches" in self) {
        const names = await self.caches.keys();
        await Promise.all(
          names
            .filter((name) => name.includes("mindroom") || name.includes("workbox"))
            .map((name) => self.caches.delete(name))
        );
      }
      await self.clients.claim();
      const clients = await self.clients.matchAll({
        includeUncontrolled: true,
        type: "window",
      });
      await Promise.all(clients.map((client) => client.navigate("{{ $target }}")));
      await self.registration.unregister();
    })()
  );
});
{{- end -}}
