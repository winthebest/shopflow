{{/* Labels. Pods carry app.kubernetes.io/name=<service> and part-of=shopflow (log pipeline contract). */}}
{{- define "shop.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: shopflow
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{- end }}

{{- define "shop.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end }}

{{/* Image reference pinned by digest; rendering fails without one. */}}
{{- define "shop.image" -}}
{{- $digest := required (printf "services.%s.image.digest is required: images run by digest only" .name) .image.digest -}}
{{- if .image.tag -}}
{{- printf "%s:%s@%s" .image.repository .image.tag $digest -}}
{{- else -}}
{{- printf "%s@%s" .image.repository $digest -}}
{{- end -}}
{{- end }}

{{/* Non-root UID/GID 10001, read-only root filesystem, no capabilities (runtime contract). */}}
{{- define "shop.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "shop.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}

{{/* Environment shared by every container: log level and OpenTelemetry. Callers may pass `otelEnabled` to
     override .Values.otel.enabled (the one-off Jobs keep the SDK off: no spans worth exporting, no exit delay). */}}
{{- define "shop.commonEnv" -}}
{{- $otelEnabled := .root.Values.otel.enabled -}}
{{- if hasKey . "otelEnabled" }}{{ $otelEnabled = .otelEnabled }}{{ end -}}
- name: LOG_LEVEL
  value: {{ .root.Values.logLevel | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ .name | quote }}
# service.instance.id = the pod name: Prometheus makes it the `instance` label, so per-process series (e.g. the
# orders circuit-state gauge) read as pods instead of the SDK's random per-process UUID. POD_NAME must come first:
# Kubernetes only expands $(VAR) references to variables defined earlier in the list.
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "deployment.environment=%s,service.instance.id=$(POD_NAME)" (required "environment is required (local or aws)" .root.Values.environment) | quote }}
- name: OTEL_SEMCONV_STABILITY_OPT_IN
  value: http
{{- if $otelEnabled }}
- name: OTEL_SDK_DISABLED
  value: "false"
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .root.Values.otel.endpoint | quote }}
{{- else }}
- name: OTEL_SDK_DISABLED
  value: "true"
{{- end }}
{{- with .svc.databaseSecret }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .name }}
      key: {{ .key }}
{{- end }}
{{- end }}

{{/* Pod spec for the migration and seed Jobs: the orders image with a one-off command. The `default`
     ServiceAccount, because a PreSync hook runs before the chart's own ServiceAccount exists; no token is mounted. */}}
{{- define "shop.jobPodSpec" -}}
{{- $svc := index .root.Values.services .job.service -}}
serviceAccountName: default
automountServiceAccountToken: false
restartPolicy: Never
securityContext:
  {{- include "shop.podSecurityContext" . | nindent 2 }}
containers:
  - name: {{ .name }}
    image: {{ include "shop.image" (dict "name" .job.service "image" $svc.image) }}
    imagePullPolicy: IfNotPresent
    command:
      {{- toYaml .job.command | nindent 6 }}
    env:
      {{- include "shop.commonEnv" (dict "root" .root "name" .name "svc" $svc "otelEnabled" false) | nindent 6 }}
    resources:
      {{- toYaml $svc.resources | nindent 6 }}
    securityContext:
      {{- include "shop.containerSecurityContext" . | nindent 6 }}
    volumeMounts:
      - name: tmp
        mountPath: /tmp
volumes:
  - name: tmp
    emptyDir:
      sizeLimit: 64Mi
{{- end }}

{{/* Which block this release renders (`component` value): `web` = app `shop` (API services, migration, seed,
     route); `worker` = app `fulfillment-worker` in profile ops (the KEDA-scaled CDC consumer, its Kafka credential
     copies and ScaledObject). Both apps read values.yaml, so one image bump covers both. */}}
{{- define "shop.renders" -}}
{{- $component := .root.Values.component -}}
{{- if not (has $component (list "web" "worker")) -}}
{{- fail (printf "component must be web or worker, got %q" $component) -}}
{{- end -}}
{{- if eq (.svc.kind | default "api") "worker" -}}
{{- if eq $component "worker" }}true{{ end -}}
{{- else if eq $component "web" -}}
true
{{- end -}}
{{- end }}

{{- define "shop.serviceAccountName" -}}
{{- if eq .Values.component "worker" }}fulfillment-worker{{ else }}shop{{ end -}}
{{- end }}

{{/* Env from Secrets (`secretEnv: {VAR: {name, key}}`), e.g. CNPG managed-role and copied Kafka credentials. */}}
{{- define "shop.secretEnv" -}}
{{- range $var, $ref := .svc.secretEnv }}
- name: {{ $var }}
  valueFrom:
    secretKeyRef:
      name: {{ $ref.name }}
      key: {{ $ref.key }}
{{- end }}
{{- end }}

{{/* Kafka connection of the worker, one source for the Deployment env and the KEDA trigger (values: worker.kafka). */}}
{{- define "shop.workerKafkaEnv" -}}
{{- $kafka := .Values.worker.kafka -}}
- name: KAFKA_BOOTSTRAP_SERVERS
  value: {{ $kafka.bootstrapServers | quote }}
- name: KAFKA_TOPIC
  value: {{ $kafka.topic | quote }}
- name: KAFKA_GROUP_ID
  value: {{ $kafka.consumerGroup | quote }}
- name: KAFKA_SECURITY_PROTOCOL
  value: SASL_SSL
- name: KAFKA_USERNAME
  value: {{ $kafka.user | quote }}
- name: KAFKA_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ $kafka.credentialsSecret }}
      key: password
- name: KAFKA_CA_FILE
  value: /etc/kafka-ca/ca.crt
{{- end }}
