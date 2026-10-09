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

{{/* Environment shared by every container: log level and OpenTelemetry (off until Phase 3). */}}
{{- define "shop.commonEnv" -}}
- name: LOG_LEVEL
  value: {{ .root.Values.logLevel | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ .name | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "deployment.environment=%s" (required "environment is required (local or aws)" .root.Values.environment) | quote }}
- name: OTEL_SEMCONV_STABILITY_OPT_IN
  value: http
{{- if .root.Values.otel.enabled }}
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
      {{- include "shop.commonEnv" (dict "root" .root "name" .name "svc" $svc) | nindent 6 }}
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
