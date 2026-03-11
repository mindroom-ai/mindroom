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
- name: MINDROOM_KUBERNETES_WORKER_TOKEN_SECRET_NAME
  value: "mindroom-api-keys-{{ $values.customer }}"
- name: MINDROOM_KUBERNETES_WORKER_TOKEN_SECRET_KEY
  value: "sandbox_proxy_token"
- name: MINDROOM_KUBERNETES_WORKER_IDLE_TIMEOUT_SECONDS
  value: {{ $values.kubernetesWorkerIdleTimeoutSeconds | quote }}
- name: MINDROOM_KUBERNETES_WORKER_READY_TIMEOUT_SECONDS
  value: {{ $values.kubernetesWorkerReadyTimeoutSeconds | quote }}
- name: MINDROOM_KUBERNETES_WORKER_NAME_PREFIX
  value: {{ $values.kubernetesWorkerNamePrefix | quote }}
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
  command: ["/app/run-sandbox-runner.sh"]
  ports:
  - containerPort: 8766
  env:
  - name: MINDROOM_SANDBOX_RUNNER_MODE
    value: "true"
  - name: MINDROOM_SANDBOX_PROXY_TOKEN
    valueFrom:
      secretKeyRef:
        name: mindroom-api-keys-{{ $values.customer }}
        key: sandbox_proxy_token
  - name: MINDROOM_CONFIG_PATH
    value: "/app/config.yaml"
  - name: MINDROOM_STORAGE_PATH
    value: {{ $values.storagePath }}
  - name: MINDROOM_SANDBOX_WORKER_ROOT
    value: "{{ $values.storagePath }}/workers"
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
    requests:
      memory: "256Mi"
      cpu: "100m"
    limits:
      memory: "1Gi"
      cpu: "500m"
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
