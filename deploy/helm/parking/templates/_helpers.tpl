{{- define "parking.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "parking.labels" -}}
app.kubernetes.io/name: parking
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: parking
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
parking.uz/env: {{ .Values.env }}
{{- end -}}

{{/* тег образа: неизменяемый {semver}-{git-sha}; latest/пустой в production запрещён */}}
{{- define "parking.imageTag" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if and (eq .Values.env "production") (or (eq $tag "latest") (not (regexMatch "^[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-f]{7,40}$" $tag))) -}}
{{- fail (printf "image.tag must be {semver}-{git-sha} in production, got %q" $tag) -}}
{{- end -}}
{{- $tag -}}
{{- end -}}

{{- define "parking.backendImage" -}}
{{ .Values.image.registry }}/{{ .Values.image.backend }}:{{ include "parking.imageTag" . }}
{{- end -}}

{{- define "parking.frontendImage" -}}
{{ .Values.image.registry }}/{{ .Values.image.frontend }}:{{ include "parking.imageTag" . }}
{{- end -}}

{{- define "parking.secretName" -}}
{{ include "parking.fullname" . }}-app
{{- end -}}

{{/* securityContext пода: non-root, seccomp RuntimeDefault */}}
{{- define "parking.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .Values.podSecurity.runAsUser }}
runAsGroup: {{ .Values.podSecurity.runAsGroup }}
fsGroup: {{ .Values.podSecurity.fsGroup }}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* securityContext контейнера: без эскалации, без capabilities, read-only root-fs */}}
{{- define "parking.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
privileged: false
capabilities:
  drop: ["ALL"]
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* переменные окружения backend: секреты из Secret, конфиг из values */}}
{{- define "parking.backendEnv" -}}
- name: PARKING_ENV
  value: {{ .Values.env | quote }}
- name: PARKING_LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: PARKING_CORS_ORIGINS
  value: {{ .Values.config.corsOrigins | quote }}
- name: PARKING_OTLP_ENDPOINT
  value: {{ .Values.config.otlpEndpoint | quote }}
- name: PARKING_OTEL_ENTRY_SAMPLE_RATIO
  value: {{ .Values.config.otelEntrySampleRatio | quote }}
- name: PARKING_OTEL_API_SAMPLE_RATIO
  value: {{ .Values.config.otelApiSampleRatio | quote }}
- name: PARKING_REDIS_SOCKET_TIMEOUT_SECONDS
  value: {{ .Values.config.redisSocketTimeoutSeconds | quote }}
- name: PARKING_PROMETHEUS_URL
  value: {{ .Values.config.prometheusUrl | quote }}
- name: PARKING_BARRIER_DISPATCH
  value: {{ .Values.config.barrierDispatch | quote }}
- name: PARKING_DB_POOL_SIZE
  value: {{ .Values.config.dbPoolSize | quote }}
- name: PARKING_REFERENCE_CACHE_TTL_SECONDS
  value: {{ .Values.config.referenceCacheTtlSeconds | quote }}
- name: PARKING_VERSION
  value: {{ include "parking.imageTag" . | quote }}
- name: PARKING_KEYS_SOURCE
  value: {{ .Values.config.keysSource | quote }}
{{- if eq .Values.config.keysSource "vault" }}
- name: PARKING_VAULT_ADDR
  value: {{ .Values.config.vault.addr | quote }}
- name: PARKING_VAULT_KV_MOUNT
  value: {{ .Values.config.vault.kvMount | quote }}
- name: PARKING_VAULT_KEYS_PATH
  value: {{ .Values.config.vault.keysPath | quote }}
- name: PARKING_VAULT_CA_FILE
  value: {{ .Values.config.vault.caFile | quote }}
{{- end }}
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
{{- end -}}

{{/* anti-affinity: реплики одного компонента — на разных узлах (required) и в разных зонах (preferred) */}}
{{- define "parking.affinity" -}}
podAntiAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    - labelSelector:
        matchLabels:
          app.kubernetes.io/instance: {{ .root.Release.Name }}
          app.kubernetes.io/component: {{ .component }}
      topologyKey: {{ .root.Values.topology.nodeKey }}
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchLabels:
            app.kubernetes.io/instance: {{ .root.Release.Name }}
            app.kubernetes.io/component: {{ .component }}
        topologyKey: {{ .root.Values.topology.zoneKey }}
{{- end -}}

{{/* Deployment backend-процесса (воркеры и Celery): .root, .name, .component, .spec (values-блок), .command, .args */}}
{{- define "parking.workerDeployment" -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "parking.fullname" .root }}-{{ .name }}
  labels:
    {{- include "parking.labels" .root | nindent 4 }}
    app.kubernetes.io/component: {{ .component }}
spec:
  replicas: {{ .spec.replicas }}
  revisionHistoryLimit: 5
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: {{ if gt (int .spec.replicas) 1 }}1{{ else }}0{{ end }}
      maxSurge: 1
  selector:
    matchLabels:
      app.kubernetes.io/instance: {{ .root.Release.Name }}
      app.kubernetes.io/component: {{ .component }}
  template:
    metadata:
      labels:
        {{- include "parking.labels" .root | nindent 8 }}
        app.kubernetes.io/component: {{ .component }}
      annotations:
        checksum/config: {{ toYaml .root.Values.config | sha256sum }}
    spec:
      serviceAccountName: {{ include "parking.fullname" .root }}-{{ .name }}
      automountServiceAccountToken: {{ .root.Values.serviceAccount.automountToken }}
      terminationGracePeriodSeconds: {{ .spec.terminationGracePeriodSeconds }}
      {{- with .root.Values.priorityClassName }}
      priorityClassName: {{ . }}
      {{- end }}
      securityContext:
        {{- include "parking.podSecurityContext" .root | nindent 8 }}
      {{- with .root.Values.image.pullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      affinity:
        {{- include "parking.affinity" (dict "root" .root "component" .component) | nindent 8 }}
      containers:
        - name: {{ .name }}
          image: {{ include "parking.backendImage" .root }}
          imagePullPolicy: {{ .root.Values.image.pullPolicy }}
          command: {{ toJson .command }}
          {{- with .args }}
          args: {{ toJson . }}
          {{- end }}
          env:
            {{- include "parking.backendEnv" .root | nindent 12 }}
            - name: PARKING_METRICS_PORT
              value: {{ if .noMetrics }}"0"{{ else }}{{ .root.Values.config.metricsPort | quote }}{{ end }}
          envFrom:
            - secretRef:
                name: {{ include "parking.secretName" .root }}
          {{- if not .noMetrics }}
          ports:
            - name: metrics
              containerPort: {{ .root.Values.config.metricsPort }}
          {{- end }}
          securityContext:
            {{- include "parking.containerSecurityContext" .root | nindent 12 }}
          {{- if not .noMetrics }}
          # воркеры: liveness = процесс отдаёт /metrics; readiness не нужен (трафик не принимают)
          livenessProbe:
            httpGet:
              path: /metrics
              port: metrics
            initialDelaySeconds: 15
            periodSeconds: 20
            failureThreshold: 3
          {{- end }}
          resources:
            {{- toYaml .spec.resources | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            {{- include "parking.caVolumeMount" .root | nindent 12 }}
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: 64Mi
        {{- include "parking.caVolume" .root | nindent 8 }}
{{- end -}}

{{/* egress к зависимостям: .np (values.networkPolicy), .providers/.barriers/.minio (bool), .noRedis */}}
{{- define "parking.dependencyEgress" -}}
- to:
    {{- range .np.postgres.cidrs }}
    - ipBlock: {cidr: {{ . | quote }}}
    {{- end }}
  ports:
    {{- range .np.postgres.ports }}
    - {protocol: TCP, port: {{ . }}}
    {{- end }}
{{- if not .noRedis }}
- to:
    {{- range .np.redis.cidrs }}
    - ipBlock: {cidr: {{ . | quote }}}
    {{- end }}
  ports:
    {{- range .np.redis.ports }}
    - {protocol: TCP, port: {{ . }}}
    {{- end }}
{{- end }}
{{- if .minio }}
- to:
    {{- range .np.minio.cidrs }}
    - ipBlock: {cidr: {{ . | quote }}}
    {{- end }}
  ports:
    - {protocol: TCP, port: {{ .np.minio.port }}}
{{- end }}
{{- if .barriers }}
- to:
    {{- range .np.barriers.cidrs }}
    - ipBlock: {cidr: {{ . | quote }}}
    {{- end }}
  ports:
    {{- range .np.barriers.ports }}
    - {protocol: TCP, port: {{ . }}}
    {{- end }}
{{- end }}
{{- if .providers }}
- to:
    {{- range .np.providers.cidrs }}
    - ipBlock:
        cidr: {{ . | quote }}
        except: {{ toJson $.np.providers.except }}
    {{- end }}
  ports:
    - {protocol: TCP, port: 443}
{{- end }}
# OTLP-коллектор, Sentry self-hosted, Pushgateway — в namespace наблюдаемости
- to:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: {{ .np.monitoringNamespace }}}
{{- end -}}

{{/* том с CA внутреннего PKI (если задан config.caConfigMap) */}}
{{- define "parking.caVolume" -}}
{{- if .Values.config.caConfigMap }}
- name: ca
  configMap:
    name: {{ .Values.config.caConfigMap }}
{{- end }}
{{- end -}}
{{- define "parking.caVolumeMount" -}}
{{- if .Values.config.caConfigMap }}
- name: ca
  mountPath: /etc/parking/ca
  readOnly: true
{{- end }}
{{- end -}}
