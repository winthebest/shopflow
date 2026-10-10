{{/* Enabled entries of a map of objects, as a sorted list of names. */}}
{{- define "shop-db.enabled" -}}
{{- $names := list -}}
{{- range $name, $item := . }}{{ if $item.enabled }}{{ $names = append $names $name }}{{ end }}{{ end -}}
{{- toJson (sortAlpha $names) -}}
{{- end }}

{{- define "shop-db.barmanObject" -}}shop-db-backup{{- end }}

{{/* "true" when WAL archiving is on: backup.enabled, and with onlyWithServerName (local) also a serverName. */}}
{{- define "shop-db.backupActive" -}}
{{- if and .Values.backup.enabled (or (not .Values.backup.onlyWithServerName) .Values.backup.serverName) -}}
true
{{- else -}}
false
{{- end -}}
{{- end }}
