package ingest

import (
	"bytes"
	"compress/gzip"
	"context"
	"testing"

	collectorlogsv1 "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	commonv1 "go.opentelemetry.io/proto/otlp/common/v1"
	logsv1 "go.opentelemetry.io/proto/otlp/logs/v1"
	resourcev1 "go.opentelemetry.io/proto/otlp/resource/v1"
	"google.golang.org/protobuf/proto"
)

func TestParsePayloadToStringAutoGzipMagicProtobuf(t *testing.T) {
	req := &collectorlogsv1.ExportLogsServiceRequest{
		ResourceLogs: []*logsv1.ResourceLogs{
			{
				Resource: &resourcev1.Resource{Attributes: []*commonv1.KeyValue{{
					Key:   "service.name",
					Value: &commonv1.AnyValue{Value: &commonv1.AnyValue_StringValue{StringValue: "svc-a"}},
				}}},
				ScopeLogs: []*logsv1.ScopeLogs{{
					LogRecords: []*logsv1.LogRecord{{
						SeverityText: "INFO",
						Body:         &commonv1.AnyValue{Value: &commonv1.AnyValue_StringValue{StringValue: "hello"}},
					}},
				}},
			},
		},
	}

	raw, err := proto.Marshal(req)
	if err != nil {
		t.Fatalf("marshal protobuf: %v", err)
	}

	var buffer bytes.Buffer
	gzipWriter := gzip.NewWriter(&buffer)
	if _, err := gzipWriter.Write(raw); err != nil {
		t.Fatalf("gzip write: %v", err)
	}
	_ = gzipWriter.Close()

	payloadText, parsedObj, parsedFormat, autoGzipMagic, parseErr := parsePayloadToString(context.Background(), buffer.Bytes(), "logs", true, 1024*1024)
	if parseErr != nil {
		t.Fatalf("parse payload failed: %v", parseErr)
	}

	if parsedFormat != "protobuf" {
		t.Fatalf("expected protobuf format, got %s", parsedFormat)
	}
	if !autoGzipMagic {
		t.Fatalf("expected auto_gzip_magic=true")
	}
	if payloadText == "" || parsedObj == nil {
		t.Fatalf("expected parsed payload")
	}
}

func TestParsePayloadToStringRejectsOversizedAutoGzipMagic(t *testing.T) {
	decodedPayload := bytes.Repeat([]byte("a"), 2048)
	var buffer bytes.Buffer
	gzipWriter := gzip.NewWriter(&buffer)
	if _, err := gzipWriter.Write(decodedPayload); err != nil {
		t.Fatalf("gzip write failed: %v", err)
	}
	_ = gzipWriter.Close()

	_, _, _, _, parseErr := parsePayloadToString(context.Background(), buffer.Bytes(), "logs", false, 64)
	if parseErr == nil {
		t.Fatalf("expected parse error for oversized auto gzip payload")
	}
	if parseErr != errDecodedBodyTooLarge {
		t.Fatalf("expected errDecodedBodyTooLarge, got %v", parseErr)
	}
}
