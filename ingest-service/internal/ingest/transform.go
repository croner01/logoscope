package ingest

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

var (
	deploymentPodPattern = regexp.MustCompile(`^(.+)-[a-f0-9]{8,10}-[a-z0-9]{5,10}$`)
	daemonSetPodPattern  = regexp.MustCompile(`^(.+?)-[a-f0-9]{8,10}(-[a-f0-9]{4,8})?$`)
	shortPodPattern      = regexp.MustCompile(`^(.+)-[a-z0-9]{5}$`)
	statefulSetPattern   = regexp.MustCompile(`^(.+)-\d+$`)
)

func isOTLPFormat(payload map[string]any) bool {
	if payload == nil {
		return false
	}
	_, hasResourceLogs := payload["resourceLogs"]
	_, hasResourceMetrics := payload["resourceMetrics"]
	_, hasResourceSpans := payload["resourceSpans"]
	return hasResourceLogs || hasResourceMetrics || hasResourceSpans
}

func buildLogBatchPayloads(logRecords []map[string]any, chunkSize int) []map[string]any {
	if chunkSize <= 0 {
		chunkSize = 1
	}
	if len(logRecords) == 0 {
		return []map[string]any{{
			"signal_type":  "logs",
			"batched":      true,
			"record_count": 0,
			"records":      []map[string]any{},
		}}
	}
	batches := make([]map[string]any, 0, (len(logRecords)+chunkSize-1)/chunkSize)
	for start := 0; start < len(logRecords); start += chunkSize {
		end := start + chunkSize
		if end > len(logRecords) {
			end = len(logRecords)
		}
		chunk := logRecords[start:end]
		batches = append(batches, map[string]any{
			"signal_type":  "logs",
			"batched":      true,
			"record_count": len(chunk),
			"records":      chunk,
		})
	}
	return batches
}

func buildNonLogQueueMessage(dataType string, payloadObj any, rawPayload string) map[string]any {
	if payloadObj == nil {
		return map[string]any{
			"signal_type": dataType,
			"raw_payload": rawPayload,
		}
	}
	return map[string]any{
		"signal_type": dataType,
		"payload":     payloadObj,
	}
}

func buildLogQueueMessages(payloadObj any, rawPayload string, metadata map[string]any) []map[string]any {
	if payloadObj == nil {
		normalized, logMeta := buildLogMeta(rawPayload, metadata, nil)
		return []map[string]any{fallbackRawLogRecord(normalized, rawPayload, logMeta)}
	}

	items := make([]any, 0)
	switch typed := payloadObj.(type) {
	case []any:
		items = append(items, typed...)
	default:
		items = append(items, payloadObj)
	}

	messages := make([]map[string]any, 0)
	for _, item := range items {
		payloadMap, ok := item.(map[string]any)
		if !ok {
			continue
		}
		if isOTLPFormat(payloadMap) {
			messages = append(messages, buildOTLPLogRecords(payloadMap, metadata)...)
			continue
		}
		messages = append(messages, transformFluentBitJSON(payloadMap, metadata))
	}

	if len(messages) == 0 {
		normalized, logMeta := buildLogMeta(rawPayload, metadata, nil)
		messages = append(messages, fallbackRawLogRecord(normalized, rawPayload, logMeta))
	}

	return messages
}

func buildOTLPLogRecords(payload map[string]any, metadata map[string]any) []map[string]any {
	resourceLogs := asSlice(payload["resourceLogs"])
	messages := make([]map[string]any, 0)
	for _, resourceLogAny := range resourceLogs {
		resourceLog := asMap(resourceLogAny)
		resource := asMap(resourceLog["resource"])
		resourceAttrs := extractAttributes(resource["attributes"])

		scopeLogs := asSlice(resourceLog["scopeLogs"])
		for _, scopeLogAny := range scopeLogs {
			scopeLog := asMap(scopeLogAny)
			logRecords := asSlice(scopeLog["logRecords"])
			for _, logRecordAny := range logRecords {
				logRecord := asMap(logRecordAny)
				messages = append(messages, transformSingleOTLPLog(resourceAttrs, logRecord, metadata))
			}
		}
	}
	return messages
}

func transformSingleOTLPLog(resource map[string]any, logRecord map[string]any, metadata map[string]any) map[string]any {
	logAttrs := extractAttributes(logRecord["attributes"])

	logBody := logRecord["body"]
	logContent := ""
	if bodyMap := asMap(logBody); len(bodyMap) > 0 {
		logContent = asString(bodyMap["stringValue"])
	}
	if strings.TrimSpace(logContent) == "" {
		logContent = asString(logBody)
	}

	existingLogMeta := asMap(logAttrs["log_meta"])
	normalizedMessage, logMeta := buildLogMeta(logContent, metadata, existingLogMeta)
	logAttrs["log_meta"] = logMeta

	attrKubernetes := asMap(logAttrs["kubernetes"])
	resourceKubernetes := asMap(resource["kubernetes"])

	podName := firstNonEmpty(
		asString(attrKubernetes["pod_name"]),
		asString(attrKubernetes["pod"]),
		asString(logAttrs["k8s.pod.name"]),
		asString(logAttrs["k8s_pod_name"]),
		asString(resourceKubernetes["pod_name"]),
		asString(resourceKubernetes["pod"]),
		asString(resource["k8s.pod.name"]),
		asString(resource["k8s_pod_name"]),
	)
	namespaceName := firstNonEmpty(
		asString(attrKubernetes["namespace_name"]),
		asString(attrKubernetes["namespace"]),
		asString(logAttrs["k8s.namespace.name"]),
		asString(logAttrs["k8s_namespace_name"]),
		asString(resourceKubernetes["namespace_name"]),
		asString(resourceKubernetes["namespace"]),
		asString(resource["k8s.namespace.name"]),
		asString(resource["k8s_namespace_name"]),
	)
	nodeName := firstNonEmpty(
		asString(attrKubernetes["host"]),
		asString(attrKubernetes["node_name"]),
		asString(attrKubernetes["node"]),
		asString(logAttrs["k8s.node.name"]),
		asString(logAttrs["k8s_node_name"]),
		asString(resourceKubernetes["host"]),
		asString(resourceKubernetes["node_name"]),
		asString(resourceKubernetes["node"]),
		asString(resource["k8s.node.name"]),
		asString(resource["k8s_node_name"]),
	)
	hostIP := firstNonEmpty(
		asString(attrKubernetes["host_ip"]),
		asString(logAttrs["k8s.host.ip"]),
		asString(logAttrs["k8s_host_ip"]),
		asString(resourceKubernetes["host_ip"]),
		asString(resource["k8s.host.ip"]),
		asString(resource["k8s_host_ip"]),
	)
	containerName := firstNonEmpty(
		asString(attrKubernetes["container_name"]),
		asString(logAttrs["k8s.container.name"]),
		asString(logAttrs["k8s_container_name"]),
		asString(resourceKubernetes["container_name"]),
		asString(resource["k8s.container.name"]),
		asString(resource["k8s_container_name"]),
	)
	containerID := firstNonEmpty(
		asString(attrKubernetes["container_id"]),
		asString(attrKubernetes["docker_id"]),
		asString(logAttrs["k8s.container.id"]),
		asString(logAttrs["k8s_container_id"]),
		asString(logAttrs["k8s.docker.id"]),
		asString(logAttrs["k8s_docker_id"]),
		asString(resourceKubernetes["container_id"]),
		asString(resourceKubernetes["docker_id"]),
		asString(resource["k8s.container.id"]),
		asString(resource["k8s_container_id"]),
	)
	containerImage := firstNonEmpty(
		asString(attrKubernetes["container_image"]),
		asString(logAttrs["k8s.container.image"]),
		asString(logAttrs["k8s_container_image"]),
		asString(resourceKubernetes["container_image"]),
		asString(resource["k8s.container.image"]),
		asString(resource["k8s_container_image"]),
	)
	podID := firstNonEmpty(
		asString(attrKubernetes["pod_id"]),
		asString(logAttrs["k8s.pod.uid"]),
		asString(logAttrs["k8s_pod_id"]),
		asString(resourceKubernetes["pod_id"]),
		asString(resource["k8s.pod.uid"]),
		asString(resource["k8s_pod_id"]),
	)
	labels := firstNonEmptyMap(
		asMap(attrKubernetes["labels"]),
		asMap(logAttrs["k8s.labels"]),
		asMap(logAttrs["k8s_labels"]),
		asMap(resourceKubernetes["labels"]),
		asMap(resource["k8s.labels"]),
		asMap(resource["k8s_labels"]),
	)

	serviceName := deriveServiceNameFromPod(podName)
	if serviceName == "" {
		serviceName = firstNonEmpty(
			asString(logAttrs["service.name"]),
			asString(logAttrs["service_name"]),
			asString(resource["service.name"]),
			asString(resource["service_name"]),
		)
	}

	traceID := normalizeOTelTraceID(firstNonEmpty(
		asString(logRecord["traceId"]),
		asString(logRecord["trace_id"]),
		asString(logAttrs["trace_id"]),
		asString(logAttrs["traceId"]),
		asString(logAttrs["trace.id"]),
	))
	spanID := normalizeOTelSpanID(firstNonEmpty(
		asString(logRecord["spanId"]),
		asString(logRecord["span_id"]),
		asString(logAttrs["span_id"]),
		asString(logAttrs["spanId"]),
		asString(logAttrs["span.id"]),
	))

	flags := asInt(logRecord["flags"], asInt(logAttrs["flags"], 0))
	traceIDSource := "missing"
	if traceID != "" {
		traceIDSource = "otlp"
		logAttrs["trace_id"] = traceID
	}
	if spanID != "" {
		logAttrs["span_id"] = spanID
	}
	if flags != 0 {
		logAttrs["flags"] = flags
	}
	logAttrs["trace_id_source"] = traceIDSource

	return map[string]any{
		"log":             normalizedMessage,
		"timestamp":       asString(logRecord["timeUnixNano"]),
		"severity":        asString(logRecord["severityText"]),
		"service.name":    serviceName,
		"trace_id":        traceID,
		"span_id":         spanID,
		"flags":           flags,
		"trace_id_source": traceIDSource,
		"attributes":      logAttrs,
		"resource":        resource,
		"kubernetes": map[string]any{
			"pod":             podName,
			"pod_name":        podName,
			"namespace":       namespaceName,
			"namespace_name":  namespaceName,
			"node":            nodeName,
			"node_name":       nodeName,
			"host":            nodeName,
			"host_ip":         hostIP,
			"container_name":  containerName,
			"container_id":    containerID,
			"docker_id":       containerID,
			"container_image": containerImage,
			"pod_id":          podID,
			"labels":          labels,
		},
	}
}

func transformFluentBitJSON(payload map[string]any, metadata map[string]any) map[string]any {
	attrs := asMap(payload["attributes"])
	payloadKubernetes := asMap(payload["kubernetes"])
	attrsKubernetes := asMap(attrs["kubernetes"])

	podName := firstNonEmpty(
		asString(payloadKubernetes["pod_name"]),
		asString(payloadKubernetes["pod"]),
		asString(attrsKubernetes["pod_name"]),
		asString(attrsKubernetes["pod"]),
		asString(attrs["k8s_pod_name"]),
		asString(attrs["k8s.pod.name"]),
	)
	namespaceName := firstNonEmpty(
		asString(payloadKubernetes["namespace_name"]),
		asString(payloadKubernetes["namespace"]),
		asString(attrsKubernetes["namespace_name"]),
		asString(attrsKubernetes["namespace"]),
		asString(attrs["k8s_namespace_name"]),
		asString(attrs["k8s.namespace.name"]),
	)
	nodeName := firstNonEmpty(
		asString(payloadKubernetes["host"]),
		asString(payloadKubernetes["node_name"]),
		asString(payloadKubernetes["node"]),
		asString(attrsKubernetes["host"]),
		asString(attrsKubernetes["node_name"]),
		asString(attrsKubernetes["node"]),
		asString(attrs["k8s_node_name"]),
		asString(attrs["k8s.node.name"]),
	)
	hostIP := firstNonEmpty(
		asString(payloadKubernetes["host_ip"]),
		asString(attrsKubernetes["host_ip"]),
		asString(attrs["k8s_host_ip"]),
		asString(attrs["k8s.host.ip"]),
	)
	containerName := firstNonEmpty(
		asString(payloadKubernetes["container_name"]),
		asString(attrsKubernetes["container_name"]),
		asString(attrs["k8s_container_name"]),
		asString(attrs["k8s.container.name"]),
	)
	containerID := firstNonEmpty(
		asString(payloadKubernetes["container_id"]),
		asString(payloadKubernetes["docker_id"]),
		asString(attrsKubernetes["container_id"]),
		asString(attrsKubernetes["docker_id"]),
		asString(attrs["k8s_container_id"]),
		asString(attrs["k8s.container.id"]),
		asString(attrs["k8s_docker_id"]),
		asString(attrs["k8s.docker.id"]),
	)
	containerImage := firstNonEmpty(
		asString(payloadKubernetes["container_image"]),
		asString(attrsKubernetes["container_image"]),
		asString(attrs["k8s_container_image"]),
		asString(attrs["k8s.container.image"]),
	)
	podID := firstNonEmpty(
		asString(payloadKubernetes["pod_id"]),
		asString(attrsKubernetes["pod_id"]),
		asString(attrs["k8s_pod_id"]),
		asString(attrs["k8s.pod.uid"]),
	)
	labels := firstNonEmptyMap(
		asMap(payloadKubernetes["labels"]),
		asMap(attrsKubernetes["labels"]),
		asMap(attrs["k8s_labels"]),
		asMap(attrs["k8s.labels"]),
		asMap(attrs["labels"]),
	)

	serviceName := deriveServiceNameFromPod(podName)
	if serviceName == "" {
		serviceName = firstNonEmpty(
			asString(payload["service.name"]),
			asString(payload["service_name"]),
			asString(attrs["service.name"]),
			asString(attrs["service_name"]),
			asString(attrs["service"]),
			asString(attrs["app"]),
		)
	}

	existingMeta := asMap(attrs["log_meta"])
	if len(existingMeta) == 0 {
		existingMeta = asMap(payload["log_meta"])
	}
	normalizedMessage, logMeta := buildLogMeta(firstNonEmpty(asString(payload["log"]), asString(payload["message"])), metadata, existingMeta)
	attributesPayload := payload
	attributesPayload["log_meta"] = logMeta

	return map[string]any{
		"log":          normalizedMessage,
		"timestamp":    asString(payload["timestamp"]),
		"severity":     firstNonEmpty(asString(payload["severity"]), asString(payload["level"])),
		"service.name": serviceName,
		"attributes":   attributesPayload,
		"resource":     map[string]any{},
		"kubernetes": map[string]any{
			"pod":             podName,
			"pod_name":        podName,
			"namespace":       namespaceName,
			"namespace_name":  namespaceName,
			"node":            nodeName,
			"node_name":       nodeName,
			"host":            nodeName,
			"host_ip":         hostIP,
			"container_name":  containerName,
			"container_id":    containerID,
			"docker_id":       containerID,
			"container_image": containerImage,
			"pod_id":          podID,
			"labels":          labels,
		},
	}
}

func fallbackRawLogRecord(normalized string, rawPayload string, logMeta map[string]any) map[string]any {
	return map[string]any{
		"log":          normalized,
		"timestamp":    "",
		"severity":     "",
		"service.name": "",
		"attributes": map[string]any{
			"raw_payload": rawPayload,
			"log_meta":    logMeta,
		},
		"resource": map[string]any{},
		"kubernetes": map[string]any{
			"pod_name":       nil,
			"namespace_name": nil,
			"node_name":      nil,
			"labels":         map[string]any{},
		},
	}
}

func buildLogMeta(content string, metadata map[string]any, existing map[string]any) (string, map[string]any) {
	normalized := normalizeLogText(content)
	merged := map[string]any{}
	for key, value := range existing {
		merged[key] = value
	}

	wrapped := false
	if wrapper, ok := tryParseWrappedLog(content); ok {
		normalized = wrapper.Message
		wrapped = true
		if wrapper.Stream != "" {
			merged["stream"] = wrapper.Stream
		}
		if wrapper.CollectorTime != "" {
			merged["collector_time"] = wrapper.CollectorTime
		}
	}

	lineCount := 0
	if normalized != "" {
		lineCount = strings.Count(normalized, "\n") + 1
	}

	merged["line_count"] = lineCount
	merged["wrapped"] = wrapped
	merged["merged"] = lineCount > 1
	if _, ok := merged["truncated"]; !ok {
		merged["truncated"] = false
	}
	if _, ok := merged["raw_size"]; !ok {
		merged["raw_size"] = len(content)
	}
	if _, ok := merged["ingest_format"]; !ok {
		merged["ingest_format"] = firstNonEmpty(asString(metadata["parsed_format"]), "unknown")
	}
	if _, ok := merged["auto_gzip_magic"]; !ok {
		merged["auto_gzip_magic"] = asBool(metadata["auto_gzip_magic"])
	}
	if _, ok := merged["parser_profile"]; !ok {
		merged["parser_profile"] = firstNonEmpty(asString(metadata["parser_profile"]), "unknown")
	}
	if _, ok := merged["confidence"]; !ok {
		if wrapped {
			merged["confidence"] = 0.95
		} else if lineCount > 1 {
			merged["confidence"] = 0.85
		} else {
			merged["confidence"] = 0.7
		}
	}

	return normalized, merged
}

type wrappedLog struct {
	Message       string
	Stream        string
	CollectorTime string
}

func tryParseWrappedLog(content string) (wrappedLog, bool) {
	trimmed := strings.TrimSpace(content)
	if trimmed == "" || !strings.HasPrefix(trimmed, "{") || !strings.HasSuffix(trimmed, "}") {
		return wrappedLog{}, false
	}
	var parsed any
	if err := json.Unmarshal([]byte(trimmed), &parsed); err != nil {
		return wrappedLog{}, false
	}
	payload := asMap(parsed)
	if len(payload) == 0 {
		return wrappedLog{}, false
	}
	message := asString(payload["log"])
	if message == "" {
		return wrappedLog{}, false
	}
	return wrappedLog{
		Message:       normalizeLogText(message),
		Stream:        asString(payload["stream"]),
		CollectorTime: asString(payload["time"]),
	}, true
}

func normalizeLogText(raw string) string {
	normalized := strings.ReplaceAll(raw, "\r\n", "\n")
	normalized = strings.ReplaceAll(normalized, "\r", "\n")
	return strings.TrimRight(normalized, "\n")
}

func deriveServiceNameFromPod(podName string) string {
	pod := candidateText(podName)
	if pod == "" {
		return ""
	}
	if match := deploymentPodPattern.FindStringSubmatch(pod); len(match) > 1 {
		return match[1]
	}
	if match := daemonSetPodPattern.FindStringSubmatch(pod); len(match) > 1 {
		return match[1]
	}
	if match := shortPodPattern.FindStringSubmatch(pod); len(match) > 1 {
		return match[1]
	}
	if match := statefulSetPattern.FindStringSubmatch(pod); len(match) > 1 {
		return match[1]
	}
	return pod
}

func candidateText(value string) string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" || strings.EqualFold(trimmed, "unknown") {
		return ""
	}
	return trimmed
}

func normalizeOTelID(raw string, expectedBytes int) string {
	text := strings.TrimSpace(raw)
	if text == "" {
		return ""
	}
	if strings.HasPrefix(strings.ToLower(text), "0x") {
		text = text[2:]
	}
	compactHex := strings.ToLower(strings.ReplaceAll(text, "-", ""))
	if len(compactHex) == expectedBytes*2 && isHex(compactHex) {
		return compactHex
	}

	candidates := []string{text, strings.NewReplacer("-", "+", "_", "/").Replace(text)}
	for _, candidate := range candidates {
		padding := len(candidate) % 4
		if padding != 0 {
			candidate += strings.Repeat("=", 4-padding)
		}
		decoded, err := base64.StdEncoding.DecodeString(candidate)
		if err != nil {
			continue
		}
		if len(decoded) == expectedBytes {
			return strings.ToLower(fmt.Sprintf("%x", decoded))
		}
	}
	return text
}

func normalizeOTelTraceID(raw string) string {
	return normalizeOTelID(raw, 16)
}

func normalizeOTelSpanID(raw string) string {
	return normalizeOTelID(raw, 8)
}

func extractAttributes(raw any) map[string]any {
	result := map[string]any{}
	for _, item := range asSlice(raw) {
		attribute := asMap(item)
		key := asString(attribute["key"])
		if key == "" {
			continue
		}
		value := extractAttributeValue(attribute["value"])
		if value != nil {
			result[key] = value
		}
	}
	return result
}

func extractAttributeValue(raw any) any {
	valueMap := asMap(raw)
	if len(valueMap) == 0 {
		return nil
	}
	if value, ok := valueMap["stringValue"]; ok {
		return asString(value)
	}
	if value, ok := valueMap["intValue"]; ok {
		return asString(value)
	}
	if value, ok := valueMap["doubleValue"]; ok {
		if parsed, err := strconv.ParseFloat(asString(value), 64); err == nil {
			return parsed
		}
		return asString(value)
	}
	if value, ok := valueMap["boolValue"]; ok {
		return asBool(value)
	}
	if value, ok := valueMap["arrayValue"]; ok {
		arrayMap := asMap(value)
		values := make([]any, 0)
		for _, item := range asSlice(arrayMap["values"]) {
			values = append(values, extractAttributeValue(item))
		}
		return values
	}
	if value, ok := valueMap["kvlistValue"]; ok {
		kvMap := asMap(value)
		return extractAttributes(kvMap["values"])
	}
	if value, ok := valueMap["bytesValue"]; ok {
		return asString(value)
	}
	return nil
}

func asSlice(value any) []any {
	if value == nil {
		return []any{}
	}
	if parsed, ok := value.([]any); ok {
		return parsed
	}
	return []any{}
}

func asMap(value any) map[string]any {
	if value == nil {
		return map[string]any{}
	}
	if parsed, ok := value.(map[string]any); ok {
		return parsed
	}
	return map[string]any{}
}

func asString(value any) string {
	if value == nil {
		return ""
	}
	switch typed := value.(type) {
	case string:
		return typed
	case json.Number:
		return typed.String()
	default:
		return strings.TrimSpace(fmt.Sprintf("%v", typed))
	}
}

func asInt(value any, fallback int) int {
	if value == nil {
		return fallback
	}
	switch typed := value.(type) {
	case int:
		return typed
	case int32:
		return int(typed)
	case int64:
		return int(typed)
	case float64:
		return int(typed)
	case json.Number:
		if parsed, err := typed.Int64(); err == nil {
			return int(parsed)
		}
	}
	if parsed, err := strconv.Atoi(asString(value)); err == nil {
		return parsed
	}
	return fallback
}

func asBool(value any) bool {
	if value == nil {
		return false
	}
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		normalized := strings.ToLower(strings.TrimSpace(typed))
		return normalized == "1" || normalized == "true" || normalized == "yes" || normalized == "on"
	default:
		return false
	}
}

func copyMap(source map[string]any) map[string]any {
	copied := map[string]any{}
	for key, value := range source {
		copied[key] = value
	}
	return copied
}

func firstNonEmptyMap(values ...map[string]any) map[string]any {
	for _, value := range values {
		if len(value) > 0 {
			return copyMap(value)
		}
	}
	return map[string]any{}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		trimmed := strings.TrimSpace(value)
		if trimmed != "" {
			return trimmed
		}
	}
	return ""
}

func isHex(value string) bool {
	if value == "" {
		return false
	}
	for _, char := range value {
		if (char >= '0' && char <= '9') || (char >= 'a' && char <= 'f') || (char >= 'A' && char <= 'F') {
			continue
		}
		return false
	}
	return true
}
