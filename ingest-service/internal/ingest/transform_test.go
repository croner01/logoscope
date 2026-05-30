package ingest

import "testing"

func TestBuildNonLogQueueMessagePreservesSignalType(t *testing.T) {
	payload := map[string]any{"resourceSpans": []any{}}
	message := buildNonLogQueueMessage("traces", payload, "{}")
	if message["signal_type"] != "traces" {
		t.Fatalf("unexpected signal_type: %v", message["signal_type"])
	}
	if _, ok := message["payload"]; !ok {
		t.Fatalf("expected payload key")
	}
}

func TestBuildLogQueueMessagesSplitOTLPRecords(t *testing.T) {
	payload := map[string]any{
		"resourceLogs": []any{
			map[string]any{
				"resource": map[string]any{
					"attributes": []any{
						map[string]any{"key": "service.name", "value": map[string]any{"stringValue": "checkout-service"}},
					},
				},
				"scopeLogs": []any{
					map[string]any{
						"logRecords": []any{
							map[string]any{"timeUnixNano": "1", "severityText": "INFO", "body": map[string]any{"stringValue": "first"}},
							map[string]any{"timeUnixNano": "2", "severityText": "ERROR", "body": map[string]any{"stringValue": "second"}},
						},
					},
				},
			},
		},
	}

	messages := buildLogQueueMessages(payload, "{}", map[string]any{})
	if len(messages) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(messages))
	}
	if messages[0]["log"] != "first" || messages[1]["log"] != "second" {
		t.Fatalf("unexpected logs: %+v", messages)
	}
}

func TestTransformFluentBitJSONUsesNestedKubernetesFields(t *testing.T) {
	payload := map[string]any{
		"log": "query-service started",
		"kubernetes": map[string]any{
			"pod_name":        "query-service-6b6cf9c6bb-9ncth",
			"namespace_name":  "islap",
			"host":            "node-a",
			"host_ip":         "10.42.0.10",
			"container_name":  "query-service",
			"container_id":    "cid-001",
			"container_image": "localhost:5000/logoscope/query-service:latest",
			"pod_id":          "pod-uid-001",
			"labels": map[string]any{
				"app": "query-service",
			},
		},
	}

	message := transformFluentBitJSON(payload, map[string]any{"parsed_format": "protobuf"})

	if message["service.name"] != "query-service" {
		t.Fatalf("expected service.name query-service, got %v", message["service.name"])
	}
	k8s := asMap(message["kubernetes"])
	if k8s["pod_name"] != "query-service-6b6cf9c6bb-9ncth" {
		t.Fatalf("expected pod_name from nested kubernetes, got %v", k8s["pod_name"])
	}
	if k8s["namespace_name"] != "islap" {
		t.Fatalf("expected namespace_name islap, got %v", k8s["namespace_name"])
	}
	if k8s["container_name"] != "query-service" {
		t.Fatalf("expected container_name query-service, got %v", k8s["container_name"])
	}
	if k8s["container_id"] != "cid-001" {
		t.Fatalf("expected container_id cid-001, got %v", k8s["container_id"])
	}
	if k8s["container_image"] != "localhost:5000/logoscope/query-service:latest" {
		t.Fatalf("expected container_image from nested kubernetes, got %v", k8s["container_image"])
	}
	if k8s["pod_id"] != "pod-uid-001" {
		t.Fatalf("expected pod_id pod-uid-001, got %v", k8s["pod_id"])
	}
	if k8s["host_ip"] != "10.42.0.10" {
		t.Fatalf("expected host_ip 10.42.0.10, got %v", k8s["host_ip"])
	}
	labels := asMap(k8s["labels"])
	if labels["app"] != "query-service" {
		t.Fatalf("expected labels.app query-service, got %v", labels["app"])
	}
}

func TestTransformSingleOTLPLogUsesKubernetesMapAttributes(t *testing.T) {
	resource := map[string]any{}
	logRecord := map[string]any{
		"body":         map[string]any{"stringValue": "frontend request"},
		"severityText": "INFO",
		"attributes": []any{
			map[string]any{
				"key": "kubernetes",
				"value": map[string]any{
					"kvlistValue": map[string]any{
						"values": []any{
							map[string]any{"key": "pod_name", "value": map[string]any{"stringValue": "frontend-86d856557d-4fslt"}},
							map[string]any{"key": "namespace_name", "value": map[string]any{"stringValue": "islap"}},
							map[string]any{"key": "host", "value": map[string]any{"stringValue": "node-a"}},
							map[string]any{"key": "host_ip", "value": map[string]any{"stringValue": "10.42.0.11"}},
							map[string]any{"key": "container_name", "value": map[string]any{"stringValue": "frontend"}},
							map[string]any{"key": "container_id", "value": map[string]any{"stringValue": "cid-frontend"}},
							map[string]any{"key": "container_image", "value": map[string]any{"stringValue": "localhost:5000/logoscope/frontend:latest"}},
							map[string]any{"key": "pod_id", "value": map[string]any{"stringValue": "pod-uid-frontend"}},
							map[string]any{
								"key": "labels",
								"value": map[string]any{
									"kvlistValue": map[string]any{
										"values": []any{
											map[string]any{"key": "app", "value": map[string]any{"stringValue": "frontend"}},
										},
									},
								},
							},
						},
					},
				},
			},
		},
	}

	message := transformSingleOTLPLog(resource, logRecord, map[string]any{"parsed_format": "protobuf"})

	if message["service.name"] != "frontend" {
		t.Fatalf("expected service.name frontend, got %v", message["service.name"])
	}
	k8s := asMap(message["kubernetes"])
	if k8s["pod_name"] != "frontend-86d856557d-4fslt" {
		t.Fatalf("expected pod_name from kubernetes map, got %v", k8s["pod_name"])
	}
	if k8s["namespace_name"] != "islap" {
		t.Fatalf("expected namespace_name islap, got %v", k8s["namespace_name"])
	}
	if k8s["container_name"] != "frontend" {
		t.Fatalf("expected container_name frontend, got %v", k8s["container_name"])
	}
	if k8s["container_id"] != "cid-frontend" {
		t.Fatalf("expected container_id cid-frontend, got %v", k8s["container_id"])
	}
	if k8s["container_image"] != "localhost:5000/logoscope/frontend:latest" {
		t.Fatalf("expected container_image frontend image, got %v", k8s["container_image"])
	}
	if k8s["pod_id"] != "pod-uid-frontend" {
		t.Fatalf("expected pod_id pod-uid-frontend, got %v", k8s["pod_id"])
	}
	if k8s["host_ip"] != "10.42.0.11" {
		t.Fatalf("expected host_ip 10.42.0.11, got %v", k8s["host_ip"])
	}
	labels := asMap(k8s["labels"])
	if labels["app"] != "frontend" {
		t.Fatalf("expected labels.app frontend, got %v", labels["app"])
	}
}
