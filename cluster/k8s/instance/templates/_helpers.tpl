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
{{- printf "%s|%s|%s|%s|%s|%s|%s|%s|%s|%s" (.openai_key | default "") (.anthropic_key | default "") (.openrouter_key | default "") (.google_key | default "") (.deepseek_key | default "") (.supabaseServiceKey | default "") (.sandbox_proxy_token | default "") (.credentials_encryption_key | default "") (.matrixOidc.clientSecret | default "") (.matrixRegistrationSharedSecret | default .matrix_admin_password | default "") | sha256sum -}}
{{- end -}}
{{- end }}

{{- /*
Tenant-scoped dedicated-worker name prefix.
Worker resource names are derived from the worker key, so every tenant in the shared
`mindroom-instances` namespace must start from its own prefix: it keeps two tenants from
generating the same worker name, and it is what the worker-manager admission policy scopes
create and update requests to.
The customer must be a DNS label so normalization leaves it unchanged and distinct customers
always get distinct prefixes. Hyphens are allowed: `mindroom-worker-a-` also starts the names of
customer `a-b`, so the policy matches the exact `{prefix}-{24 hex digest}` shape instead of a bare
prefix, and a digest never contains the hyphen that separates `b` from its own digest.
Normalization mirrors `_digest_and_safe_prefix` in `mindroom.tool_system.worker_routing`, and the
38-character limit is the longest prefix `worker_id_for_key` keeps beside its 24-character digest
in a 63-character name; a longer prefix would be truncated at runtime and could collide.
*/ -}}
{{- define "mindroom.workerNamePrefix" -}}
{{- $customer := toString .customer -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $customer) -}}
{{- fail "workerBackend=kubernetes requires a customer that is a DNS label (lowercase letters, digits and inner hyphens) so worker names stay tenant-scoped" -}}
{{- end -}}
{{- $prefix := printf "%s-%s" (.kubernetesWorkerNamePrefix | default "mindroom-worker") $customer -}}
{{- $normalized := trimAll "-" (regexReplaceAll "[^a-z0-9-]+" (lower $prefix) "-") -}}
{{- if or (eq $normalized "") (gt (len $normalized) 38) -}}
{{- fail "kubernetesWorkerNamePrefix combined with customer must normalize to 1-38 characters of [a-z0-9-]" -}}
{{- end -}}
{{- $normalized -}}
{{- end }}

{{- define "mindroom.workerBackendEnv" -}}
{{- $workerBackend := .workerBackend -}}
{{- $instanceNamespace := .instanceNamespace -}}
{{- $workerImage := .workerImage -}}
{{- $workerImagePullPolicy := .workerImagePullPolicy -}}
{{- $workerServiceAccountName := .workerServiceAccountName -}}
{{- $controlPlaneNodeName := .controlPlaneNodeName -}}
{{- $values := .values -}}
{{- if eq $workerBackend "static_runner" }}
- name: MINDROOM_SANDBOX_PROXY_URL
  value: "http://localhost:8766"
{{- else if eq $workerBackend "kubernetes" }}
{{- $workerSeccomp := $values.kubernetesWorkerSeccompProfile -}}
{{- if $workerSeccomp -}}
{{- if or (not (kindIs "map" $workerSeccomp)) (ne (len $workerSeccomp) 2) (not (hasKey $workerSeccomp "type")) (not (hasKey $workerSeccomp "localhostProfile")) -}}
{{- fail "kubernetesWorkerSeccompProfile must define exactly type and localhostProfile" -}}
{{- end -}}
{{- if ne $workerSeccomp.type "Localhost" -}}
{{- fail "kubernetesWorkerSeccompProfile.type must be Localhost" -}}
{{- end -}}
{{- if not (kindIs "string" $workerSeccomp.localhostProfile) -}}
{{- fail "kubernetesWorkerSeccompProfile.localhostProfile must be a relative path" -}}
{{- end -}}
{{- if or (not (regexMatch "^[^/\\\\]+(/[^/\\\\]+)*$" $workerSeccomp.localhostProfile)) (has "." (splitList "/" $workerSeccomp.localhostProfile)) (has ".." (splitList "/" $workerSeccomp.localhostProfile)) -}}
{{- fail "kubernetesWorkerSeccompProfile.localhostProfile must be a relative path without traversal segments" -}}
{{- end -}}
{{- end -}}
- name: MINDROOM_WORKER_COMPUTER_ENABLED
  value: {{ $values.workerComputerEnabled | default false | quote }}
- name: MINDROOM_KUBERNETES_WORKER_NAMESPACE
  value: {{ $instanceNamespace | quote }}
- name: MINDROOM_KUBERNETES_WORKER_IMAGE
  value: {{ $workerImage | quote }}
- name: MINDROOM_KUBERNETES_WORKER_IMAGE_PULL_POLICY
  value: {{ $workerImagePullPolicy | quote }}
- name: MINDROOM_KUBERNETES_WORKER_PORT
  value: {{ $values.kubernetesWorkerPort | quote }}
- name: MINDROOM_KUBERNETES_WORKER_SERVICE_ACCOUNT_NAME
  value: {{ $workerServiceAccountName | quote }}
- name: MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME
  value: "mindroom-storage-{{ $values.customer }}"
- name: MINDROOM_KUBERNETES_WORKER_STORAGE_MOUNT_PATH
  value: {{ $values.storagePath | quote }}
- name: MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX
  value: {{ $values.kubernetesWorkerStorageSubpathPrefix | quote }}
- name: MINDROOM_KUBERNETES_WORKER_CONFIG_MAP_NAME
  value: "mindroom-config-{{ $values.customer }}"
- name: MINDROOM_KUBERNETES_WORKER_CONFIG_KEY
  value: "config.yaml"
- name: MINDROOM_KUBERNETES_WORKER_CONFIG_PATH
  value: "/app/config.yaml"
{{- if $controlPlaneNodeName }}
- name: MINDROOM_KUBERNETES_WORKER_NODE_NAME
  value: {{ $controlPlaneNodeName | quote }}
{{- end }}
- name: MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS
  value: {{ $values.kubernetesWorkerIdleTimeoutSeconds | quote }}
- name: MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS
  value: {{ $values.kubernetesWorkerReadyTimeoutSeconds | quote }}
- name: MINDROOM_KUBERNETES_WORKER_NAME_PREFIX
  value: {{ include "mindroom.workerNamePrefix" $values | quote }}
- name: MINDROOM_KUBERNETES_WORKER_ENABLE_SERVICE_LINKS
  value: {{ $values.kubernetesWorkerEnableServiceLinks | quote }}
{{- with $values.kubernetesWorkerRuntimeClassName }}
- name: MINDROOM_KUBERNETES_WORKER_RUNTIME_CLASS_NAME
  value: {{ . | quote }}
{{- end }}
{{- with $workerSeccomp }}
- name: MINDROOM_KUBERNETES_WORKER_SECCOMP_PROFILE_JSON
  value: {{ toJson . | quote }}
{{- end }}
- name: MINDROOM_KUBERNETES_WORKER_AUTH_SECRET_NAME
  value: "mindroom-worker-auth-{{ $values.customer }}"
- name: MINDROOM_KUBERNETES_WORKER_LABELS_JSON
  value: {{ dict "customer" $values.customer | toJson | quote }}
- name: MINDROOM_KUBERNETES_WORKER_OWNER_DEPLOYMENT_NAME
  value: "mindroom-{{ $values.customer }}"
{{- end }}
{{- end }}

{{- define "mindroom.staticRunnerContainer" -}}
{{- $values := .values -}}
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
  - name: MINDROOM_CREDENTIALS_ENCRYPTION_KEY
    valueFrom:
      secretKeyRef:
        name: {{ include "mindroom.instanceSecretName" $values }}
        key: credentials_encryption_key
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
