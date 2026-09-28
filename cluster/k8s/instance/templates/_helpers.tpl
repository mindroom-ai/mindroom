{{- define "mindroom.instanceSecretName" -}}
{{- $instanceSecrets := .instanceSecrets | default (dict) -}}
{{- $instanceSecrets.name | default (printf "mindroom-api-keys-%s" (toString .customer)) -}}
{{- end }}

{{- define "mindroom.instanceSecretsCreate" -}}
{{- $instanceSecrets := .instanceSecrets | default (dict) -}}
{{- $create := true -}}
{{- if hasKey $instanceSecrets "create" -}}
{{- $createValue := lower (toString $instanceSecrets.create) -}}
{{- $create = has $createValue (list "1" "true" "yes" "on") -}}
{{- end -}}
{{- $create -}}
{{- end }}

{{- define "mindroom.instanceSecretHash" -}}
{{- $instanceSecrets := .instanceSecrets | default (dict) -}}
{{- if $instanceSecrets.hash -}}
{{- $instanceSecrets.hash -}}
{{- else -}}
{{- printf "%s|%s|%s|%s|%s|%s|%s|%s|%s|%s" (.openai_key | default "") (.anthropic_key | default "") (.openrouter_key | default "") (.google_key | default "") (.deepseek_key | default "") (.sandbox_proxy_token | default "") (.credentials_encryption_key | default "") (.matrixOidc.clientSecret | default "") (.matrixRegistrationSharedSecret | default .matrix_admin_password | default "") (.platformSsoSecret | default "") | sha256sum -}}
{{- end -}}
{{- end }}

{{- define "mindroom.ingressControllerPeer" -}}
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: {{ required "ingressControllerNamespace is required" .Values.ingressControllerNamespace | quote }}
  podSelector:
    matchLabels:
      app.kubernetes.io/component: controller
      app.kubernetes.io/name: ingress-nginx
{{- end }}

{{- define "mindroom.staticRunnerContainer" -}}
{{- $values := .values -}}
{{- /*
The runner executes agent tool code, so it must not see the tenant credential store, Matrix state,
the live config the primary hot-reloads, or the credentials encryption key.
It mounts only agent state from the PVC over its own private storage root,
and saved tool settings reach it as per-call leases from the primary.
Its /app/config.yaml is only the seed; each request carries the primary's live config without secrets.
*/ -}}
- name: sandbox-runner
  image: {{ $values.mindroom_image | default "ghcr.io/mindroom-ai/mindroom:latest" }}
  imagePullPolicy: {{ $values.mindroom_image_pull_policy | default "Always" }}
  command: ["tini", "--", "/app/run-sandbox-runner.sh"]
  ports:
  - containerPort: 8766
  env:
  - name: MINDROOM_SANDBOX_RUNNER_MODE
    value: "true"
  - name: MINDROOM_SANDBOX_PROXY_TOKEN
    valueFrom:
      secretKeyRef:
        name: {{ include "mindroom.instanceSecretName" $values }}
        key: sandbox_proxy_token
  - name: MINDROOM_CONFIG_PATH
    value: "/app/config.yaml"
  - name: MINDROOM_STORAGE_PATH
    value: {{ $values.storagePath }}
  - name: HOME
    value: {{ $values.storagePath }}
  volumeMounts:
  - name: config
    mountPath: /app/config.yaml
    subPath: config.yaml
    readOnly: true
  - name: storage
    mountPath: {{ $values.storagePath }}
    subPath: sandbox-runner
  - name: storage
    mountPath: {{ $values.storagePath }}/agents
    subPath: agents
  - name: storage
    mountPath: {{ $values.storagePath }}/private_instances
    subPath: private_instances
  - name: sandbox-workspace
    mountPath: /app/workspace
  resources:
    {{- toYaml ($values.sandboxRunnerResources | default (dict)) | nindent 4 }}
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL
{{- end }}

{{- define "mindroom.staticRunnerVolume" -}}
- name: sandbox-workspace
  emptyDir: {}
{{- end }}
