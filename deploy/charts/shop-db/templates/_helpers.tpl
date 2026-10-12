{{/* Enabled entries of a map of objects, as a sorted list of names. */}}
{{- define "shop-db.enabled" -}}
{{- $names := list -}}
{{- range $name, $item := . }}{{ if $item.enabled }}{{ $names = append $names $name }}{{ end }}{{ end -}}
{{- toJson (sortAlpha $names) -}}
{{- end }}

{{- define "shop-db.barmanObject" -}}shop-db-backup{{- end }}

{{/* "true" with more than one instance unless logicalSlotSync is off: the standbys keep a synchronized copy of every
logical slot created with failover (PG17 sync_replication_slots; CNPG manages synchronized_standby_slots). Values may
arrive as strings from session params. */}}
{{- define "shop-db.logicalSlotSync" -}}
{{- $instances := int (.Values.instances | default 1) -}}
{{- $off := has (lower (toString .Values.logicalSlotSync)) (list "false" "off" "0" "no") -}}
{{- if and (gt $instances 1) (not $off) -}}
true
{{- else -}}
false
{{- end -}}
{{- end }}

{{/* "true" when WAL archiving is on: backup.enabled, and with onlyWithServerName (local) also a serverName. */}}
{{- define "shop-db.backupActive" -}}
{{- if and .Values.backup.enabled (or (not .Values.backup.onlyWithServerName) .Values.backup.serverName) -}}
true
{{- else -}}
false
{{- end -}}
{{- end }}
