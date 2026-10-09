{{/* Enabled entries of a map of objects, as a sorted list of names. */}}
{{- define "shop-db.enabled" -}}
{{- $names := list -}}
{{- range $name, $item := . }}{{ if $item.enabled }}{{ $names = append $names $name }}{{ end }}{{ end -}}
{{- toJson (sortAlpha $names) -}}
{{- end }}

{{- define "shop-db.barmanObject" -}}shop-db-backup{{- end }}
