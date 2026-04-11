{{- define "logoscope.namespace" -}}
{{- $ns := coalesce .Values.ns .Values.namespace.nameOverride .Values.global.namespaceOverride -}}
{{- if $ns -}}
{{- $ns -}}
{{- else -}}
{{- .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{- define "logoscope.image" -}}
{{- $ctx := .ctx -}}
{{- $key := .key -}}
{{- $global := default dict $ctx.Values.global -}}
{{- $images := default dict $ctx.Values.images -}}
{{- $components := default dict $ctx.Values.components -}}
{{- $baseCfg := index $images $key | default dict -}}
{{- $componentCfg := index $components $key | default dict -}}
{{- $componentImageCfg := index $componentCfg "image" | default dict -}}

{{- $full := coalesce (index $componentImageCfg "full") (index $baseCfg "full") -}}
{{- if $full -}}
{{- $full -}}
{{- else -}}
{{- $repository := coalesce (index $componentImageCfg "repository") (index $baseCfg "repository") -}}
{{- if eq $repository "" -}}
{{- fail (printf "images.%s.repository is required" $key) -}}
{{- end -}}
{{- $registry := coalesce (index $componentImageCfg "registry") (index $baseCfg "registry") (index $global "imageRegistry") -}}
{{- $tag := coalesce (index $componentImageCfg "tag") (index $baseCfg "tag") (index $global "imageTag") -}}
{{- $image := $repository -}}
{{- if $registry -}}
{{- $image = printf "%s/%s" (trimSuffix "/" $registry) (trimPrefix "/" $repository) -}}
{{- end -}}
{{- if $tag -}}
{{- $image = printf "%s:%s" $image $tag -}}
{{- end -}}
{{- $image -}}
{{- end -}}
{{- end -}}

{{- define "logoscope.injectNodeSelector" -}}
{{- $manifest := .manifest -}}
{{- $nodeSelector := default dict .nodeSelector -}}
{{- if eq (len $nodeSelector) 0 -}}
{{- $manifest -}}
{{- else -}}
{{- $nodeSelectorYaml := trimSuffix "\n" (toYaml $nodeSelector) -}}
{{- $withCronjobPods := regexReplaceAll "(?m)^ {10}containers:\\n" $manifest (printf "          nodeSelector:\n%s\n          containers:\n" ($nodeSelectorYaml | indent 12)) -}}
{{- regexReplaceAll "(?m)^ {6}containers:\\n" $withCronjobPods (printf "      nodeSelector:\n%s\n      containers:\n" ($nodeSelectorYaml | indent 8)) -}}
{{- end -}}
{{- end -}}

{{- define "logoscope.applyEnvOverrides" -}}
{{- $manifest := .manifest -}}
{{- $env := default dict .env -}}
{{- $rendered := $manifest -}}
{{- range $name, $value := $env -}}
{{- $namePattern := regexQuoteMeta $name -}}
{{- $escapedValue := replace "$" "$$" (printf "%v" $value) -}}
{{- $pattern := printf "(?m)^(\\s*)- name:\\s*%s\\s*\\n\\s*value:\\s*(\"[^\"]*\"|[^\\n#]+)" $namePattern -}}
{{- $replacement := printf "${1}- name: %s\n${1}  value: %q" $name $escapedValue -}}
{{- $rendered = regexReplaceAll $pattern $rendered $replacement -}}
{{- end -}}
{{- $rendered -}}
{{- end -}}

{{- define "logoscope.renderManifest" -}}
{{- $ctx := .ctx -}}
{{- $raw := default "" .raw -}}
{{- $component := default "" .component -}}
{{- $components := default dict $ctx.Values.components -}}
{{- $componentCfg := index $components $component | default dict -}}
{{- $componentServiceCfg := index $componentCfg "service" | default dict -}}
{{- if not $raw -}}
{{- fail (printf "manifest is empty for component: %s" $component) -}}
{{- end -}}

{{- $rendered := $raw -}}

{{- if eq $component "otelCollector" -}}
{{- $rendered = replace "---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: islap\n---\n" "" $rendered -}}
{{- $rendered = replace "---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: islap\n" "" $rendered -}}
{{- end -}}

{{- $targetNs := include "logoscope.namespace" $ctx -}}
{{- $rendered = replace "namespace: islap" (printf "namespace: %s" $targetNs) $rendered -}}
{{- $rendered = replace ".islap.svc.cluster.local" (printf ".%s.svc.cluster.local" $targetNs) $rendered -}}

{{- if eq $component "aiService" -}}
{{- $rendered = replace "localhost:5000/logoscope/ai-service:latest" (include "logoscope.image" (dict "ctx" $ctx "key" "aiService")) $rendered -}}
{{- else if eq $component "execService" -}}
{{- $rendered = replace "localhost:5000/logoscope/exec-service:latest" (include "logoscope.image" (dict "ctx" $ctx "key" "execService")) $rendered -}}
{{- else if eq $component "opa" -}}
{{- $rendered = replace "docker.io/openpolicyagent/opa:0.63.0" (include "logoscope.image" (dict "ctx" $ctx "key" "opa")) $rendered -}}
{{- else if eq $component "clickhouse" -}}
{{- $rendered = replace "localhost:5000/logoscope/clickhouse-server:25.6" (include "logoscope.image" (dict "ctx" $ctx "key" "clickhouse")) $rendered -}}
{{- else if eq $component "fluentBit" -}}
{{- $rendered = replace "localhost:5000/logoscope/fluent-bit:3.1.3" (include "logoscope.image" (dict "ctx" $ctx "key" "fluentBit")) $rendered -}}
{{- else if eq $component "frontend" -}}
{{- $rendered = replace "localhost:5000/logoscope/frontend:latest" (include "logoscope.image" (dict "ctx" $ctx "key" "frontend")) $rendered -}}
{{- else if eq $component "ingestService" -}}
{{- $rendered = replace "localhost:5000/logoscope/ingest-service:latest" (include "logoscope.image" (dict "ctx" $ctx "key" "ingestService")) $rendered -}}
{{- else if eq $component "neo4j" -}}
{{- $rendered = replace "localhost:5000/logoscope/neo4j:4.4-community" (include "logoscope.image" (dict "ctx" $ctx "key" "neo4j")) $rendered -}}
{{- else if eq $component "otelCollector" -}}
{{- $rendered = replace "otel/opentelemetry-collector-contrib:0.91.0" (include "logoscope.image" (dict "ctx" $ctx "key" "otelCollector")) $rendered -}}
{{- else if eq $component "otelGateway" -}}
{{- $rendered = replace "localhost:5000/logoscope/opentelemetry-collector:0.111.0" (include "logoscope.image" (dict "ctx" $ctx "key" "otelGateway")) $rendered -}}
{{- else if eq $component "queryService" -}}
{{- $rendered = replace "localhost:5000/logoscope/query-service:latest" (include "logoscope.image" (dict "ctx" $ctx "key" "queryService")) $rendered -}}
{{- else if eq $component "kafka" -}}
{{- $rendered = replace "docker.io/bitnamilegacy/kafka:4.0.0-debian-12-r10" (include "logoscope.image" (dict "ctx" $ctx "key" "kafka")) $rendered -}}
{{- else if eq $component "semanticEngine" -}}
{{- $rendered = replace "localhost:5000/logoscope/semantic-engine:latest" (include "logoscope.image" (dict "ctx" $ctx "key" "semanticEngine")) $rendered -}}
{{- else if eq $component "semanticEngineWorker" -}}
{{- $rendered = replace "localhost:5000/logoscope/semantic-engine:latest" (include "logoscope.image" (dict "ctx" $ctx "key" "semanticEngineWorker")) $rendered -}}
{{- else if eq $component "topologyService" -}}
{{- $rendered = replace "localhost:5000/logoscope/topology-service:latest" (include "logoscope.image" (dict "ctx" $ctx "key" "topologyService")) $rendered -}}
{{- else if eq $component "temporal" -}}
{{- $rendered = replace "temporalio/auto-setup:1.25.2" (include "logoscope.image" (dict "ctx" $ctx "key" "temporal")) $rendered -}}
{{- $rendered = replace "postgres:16-alpine" (include "logoscope.image" (dict "ctx" $ctx "key" "temporalPostgresql")) $rendered -}}
{{- else if eq $component "valueKpiCronjob" -}}
{{- $rendered = replace "localhost:5000/logoscope/query-service:latest" (include "logoscope.image" (dict "ctx" $ctx "key" "valueKpiCronjob")) $rendered -}}
{{- else if eq $component "redis" -}}
{{- $rendered = replace "redis:7-alpine" (include "logoscope.image" (dict "ctx" $ctx "key" "redis")) $rendered -}}
{{- end -}}

{{- range $from, $to := $ctx.Values.imageReplacements -}}
{{- $rendered = replace $from $to $rendered -}}
{{- end -}}

{{- $componentEnv := index $componentCfg "env" | default dict -}}
{{- $rendered = include "logoscope.applyEnvOverrides" (dict "manifest" $rendered "env" $componentEnv) -}}

{{- if eq $component "frontend" -}}
{{- $frontendSvcType := index $componentServiceCfg "type" | default "" -}}
{{- if $frontendSvcType -}}
{{- $rendered = replace "type: NodePort" (printf "type: %s" $frontendSvcType) $rendered -}}
{{- if ne (upper $frontendSvcType) "NODEPORT" -}}
{{- $rendered = regexReplaceAll "(?m)^\\s*nodePort:\\s*\\d+\\n" $rendered "" -}}
{{- end -}}
{{- end -}}
{{- $frontendNodePort := int (index $componentServiceCfg "nodePort" | default 0) -}}
{{- if gt $frontendNodePort 0 -}}
{{- $rendered = regexReplaceAll "(?m)^\\s*nodePort:\\s*\\d+" $rendered (printf "    nodePort: %d" $frontendNodePort) -}}
{{- end -}}
{{- end -}}

{{- $replicaCount := int (index $componentCfg "replicaCount" | default 0) -}}
{{- if gt $replicaCount 0 -}}
{{- $rendered = regexReplaceAll "(?m)^  replicas:\\s*\\d+" $rendered (printf "  replicas: %d" $replicaCount) -}}
{{- end -}}

{{- $hpaCfg := index $componentCfg "hpa" | default dict -}}
{{- $hpaMin := int (index $hpaCfg "minReplicas" | default 0) -}}
{{- if gt $hpaMin 0 -}}
{{- $rendered = regexReplaceAll "(?m)^  minReplicas:\\s*\\d+" $rendered (printf "  minReplicas: %d" $hpaMin) -}}
{{- end -}}
{{- $hpaMax := int (index $hpaCfg "maxReplicas" | default 0) -}}
{{- if gt $hpaMax 0 -}}
{{- $rendered = regexReplaceAll "(?m)^  maxReplicas:\\s*\\d+" $rendered (printf "  maxReplicas: %d" $hpaMax) -}}
{{- end -}}

{{- if eq $component "valueKpiCronjob" -}}
{{- $schedule := index $componentCfg "schedule" | default "" -}}
{{- if $schedule -}}
{{- $rendered = regexReplaceAll "(?m)^  schedule:\\s*\"[^\"]*\"" $rendered (printf "  schedule: %q" $schedule) -}}
{{- end -}}
{{- end -}}

{{- $global := default dict $ctx.Values.global -}}
{{- $storageClass := coalesce (index $componentCfg "storageClass") (index $global "storageClass") -}}
{{- if $storageClass -}}
{{- $rendered = regexReplaceAll "storageClassName:[[:space:]]*[^\\n]+" $rendered (printf "storageClassName: %s" $storageClass) -}}
{{- end -}}

{{- $globalNodeSelector := index $global "nodeSelector" | default dict -}}
{{- $componentNodeSelector := index $componentCfg "nodeSelector" | default dict -}}
{{- $effectiveNodeSelector := dict -}}
{{- range $k, $v := $globalNodeSelector -}}
{{- $_ := set $effectiveNodeSelector $k $v -}}
{{- end -}}
{{- range $k, $v := $componentNodeSelector -}}
{{- $_ := set $effectiveNodeSelector $k $v -}}
{{- end -}}

{{- $rendered = include "logoscope.injectNodeSelector" (dict "manifest" $rendered "nodeSelector" $effectiveNodeSelector) -}}

{{- tpl $rendered $ctx -}}
{{- end -}}
