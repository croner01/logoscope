{{- define "logoscope.namespace" -}}
{{- .Release.Namespace -}}
{{- end -}}

{{- define "logoscope.renderManifest" -}}
{{- $ctx := .ctx -}}
{{- $path := .path -}}
{{- $raw := $ctx.Files.Get $path -}}
{{- if not $raw -}}
{{- fail (printf "manifest file not found: %s" $path) -}}
{{- end -}}

{{- $rendered := $raw -}}

{{- if eq $path "files/manifests/otel-collector.yaml" -}}
{{- $rendered = replace "---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: islap\n---\n" "" $rendered -}}
{{- $rendered = replace "---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: islap\n" "" $rendered -}}
{{- end -}}

{{- $targetNs := include "logoscope.namespace" $ctx -}}
{{- $rendered = replace "namespace: islap" (printf "namespace: %s" $targetNs) $rendered -}}
{{- $rendered = replace ".islap.svc.cluster.local" (printf ".%s.svc.cluster.local" $targetNs) $rendered -}}

{{- range $from, $to := $ctx.Values.imageReplacements -}}
{{- $rendered = replace $from $to $rendered -}}
{{- end -}}

{{- tpl $rendered $ctx -}}
{{- end -}}
