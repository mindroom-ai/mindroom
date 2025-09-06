# yamllint disable-file
---
# MindRoom Deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mindroom-{{ .Values.customer }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mindroom-{{ .Values.customer }}
  template:
    metadata:
      labels:
        app: mindroom-{{ .Values.customer }}
    spec:
      containers:
      - name: mindroom
        image: {{ .Values.mindroom_image }}
        command: ["./run-ui.sh"]
        ports:
        - containerPort: 3003
        - containerPort: 8765
        env:
        - name: DOCKER_CONTAINER
          value: "1"
        - name: OPENAI_API_KEY
          value: {{ .Values.openai_key | quote }}
        - name: ANTHROPIC_API_KEY
          value: {{ .Values.anthropic_key | quote }}
        - name: MATRIX_HOMESERVER
          value: "http://synapse-{{ .Values.customer }}:8008"
        - name: MATRIX_SERVER_NAME
          value: "{{ .Values.customer }}.matrix.{{ .Values.baseDomain }}"
        volumeMounts:
        - name: data
          mountPath: /app/mindroom_data
        - name: config
          mountPath: /app/config.yaml
          subPath: config.yaml
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: mindroom-{{ .Values.customer }}-data
      - name: config
        configMap:
          name: mindroom-{{ .Values.customer }}-config
---
# Synapse Matrix Server Deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: synapse-{{ .Values.customer }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: synapse-{{ .Values.customer }}
  template:
    metadata:
      labels:
        app: synapse-{{ .Values.customer }}
    spec:
      containers:
      - name: synapse
        image: {{ .Values.synapse_image }}
        ports:
        - containerPort: 8008
        - containerPort: 8448
        env:
        - name: SYNAPSE_CONFIG_PATH
          value: "/data/homeserver.yaml"
        - name: SYNAPSE_SERVER_NAME
          value: "m-{{ .Values.domain }}"
        - name: SYNAPSE_REPORT_STATS
          value: "no"
        volumeMounts:
        - name: synapse-data
          mountPath: /data
        - name: synapse-config
          mountPath: /data/homeserver.yaml
          subPath: homeserver.yaml
        - name: synapse-log
          mountPath: /data/log.config
          subPath: log.config
      volumes:
      - name: synapse-data
        persistentVolumeClaim:
          claimName: synapse-{{ .Values.customer }}-data
      - name: synapse-config
        configMap:
          name: synapse-{{ .Values.customer }}-config
      - name: synapse-log
        configMap:
          name: synapse-{{ .Values.customer }}-log
---
# MindRoom Service
apiVersion: v1
kind: Service
metadata:
  name: mindroom-{{ .Values.customer }}
spec:
  ports:
  - name: frontend
    port: 3003
  - name: backend
    port: 8765
  selector:
    app: mindroom-{{ .Values.customer }}
---
# Synapse Service
apiVersion: v1
kind: Service
metadata:
  name: synapse-{{ .Values.customer }}
spec:
  ports:
  - name: client
    port: 8008
  - name: federation
    port: 8448
  selector:
    app: synapse-{{ .Values.customer }}
---
# MindRoom PVC
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mindroom-{{ .Values.customer }}-data
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: {{ .Values.storage }}
---
# Synapse PVC
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: synapse-{{ .Values.customer }}-data
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi
---
# MindRoom ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: mindroom-{{ .Values.customer }}-config
data:
  config.yaml: |
    # Minimal config with Matrix integration
    agents:
      general:
        display_name: GeneralAgent
        model: default
        role: General assistant
        rooms: [lobby]
      router:
        display_name: Router
        model: default
        role: Routes messages to appropriate agents
        rooms: [lobby]
    models:
      default:
        provider: openai
        id: gpt-4o-mini
    defaults:
      markdown: true
    authorization:
      global_users:
        - '@admin:m-{{ .Values.domain }}'
      default_room_access: false
---
# Synapse ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: synapse-{{ .Values.customer }}-config
data:
  homeserver.yaml: |
    # Minimal Synapse config with SQLite
    server_name: "m-{{ .Values.domain }}"
    pid_file: /data/homeserver.pid

    listeners:
      - port: 8008
        tls: false
        type: http
        x_forwarded: true
        resources:
          - names: [client, federation]
            compress: false

    database:
      name: sqlite3
      args:
        database: /data/homeserver.db

    media_store_path: /data/media_store
    uploads_path: /data/uploads

    registration_shared_secret: "{{ .Values.matrix_admin_password }}"
    enable_registration: true
    enable_registration_without_verification: true

    report_stats: false
    macaroon_secret_key: "{{ randAlphaNum 32 }}"
    form_secret: "{{ randAlphaNum 32 }}"
    signing_key_path: /data/signing.key

    trusted_key_servers:
      - server_name: "matrix.org"

    # Disable email
    enable_notifs: false

    # Simple registration
    enable_registration_captcha: false
    allow_guest_access: true

    # Logging
    log_config: /data/log.config
---
# Synapse Log Config
apiVersion: v1
kind: ConfigMap
metadata:
  name: synapse-{{ .Values.customer }}-log
data:
  log.config: |
    version: 1
    formatters:
      simple:
        format: '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    handlers:
      console:
        class: logging.StreamHandler
        formatter: simple
    root:
      level: INFO
      handlers: [console]
---
