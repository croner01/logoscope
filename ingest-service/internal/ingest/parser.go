package ingest

import (
	"bytes"
	"compress/gzip"
	"compress/zlib"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	collectorlogsv1 "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	collectormetricsv1 "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	collectortracev1 "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

var errUnsupportedContentEncoding = errors.New("unsupported content-encoding")
var errDecodedBodyTooLarge = errors.New("decoded body too large")

func decodeBodyByContentEncoding(request *http.Request, body []byte, maxDecodedBytes int64) ([]byte, error) {
	encoding := strings.TrimSpace(request.Header.Get("Content-Encoding"))
	if encoding == "" {
		if maxDecodedBytes > 0 && int64(len(body)) > maxDecodedBytes {
			return nil, errDecodedBodyTooLarge
		}
		return body, nil
	}

	parts := strings.Split(encoding, ",")
	decoded := body
	for index := len(parts) - 1; index >= 0; index-- {
		part := strings.ToLower(strings.TrimSpace(parts[index]))
		switch part {
		case "", "identity":
			continue
		case "gzip", "x-gzip":
			reader, err := gzip.NewReader(bytes.NewReader(decoded))
			if err != nil {
				return nil, fmt.Errorf("invalid gzip payload: %w", err)
			}
			raw, readErr := readAllWithLimit(reader, maxDecodedBytes)
			_ = reader.Close()
			if readErr != nil {
				if errors.Is(readErr, errDecodedBodyTooLarge) {
					return nil, errDecodedBodyTooLarge
				}
				return nil, fmt.Errorf("invalid gzip payload: %w", readErr)
			}
			decoded = raw
		case "deflate":
			reader, err := zlib.NewReader(bytes.NewReader(decoded))
			if err != nil {
				return nil, fmt.Errorf("invalid deflate payload: %w", err)
			}
			raw, readErr := readAllWithLimit(reader, maxDecodedBytes)
			_ = reader.Close()
			if readErr != nil {
				if errors.Is(readErr, errDecodedBodyTooLarge) {
					return nil, errDecodedBodyTooLarge
				}
				return nil, fmt.Errorf("invalid deflate payload: %w", readErr)
			}
			decoded = raw
		default:
			return nil, fmt.Errorf("%w: %s", errUnsupportedContentEncoding, part)
		}
	}
	return decoded, nil
}

func readAllWithLimit(reader io.Reader, maxBytes int64) ([]byte, error) {
	if maxBytes <= 0 {
		return io.ReadAll(reader)
	}
	limited := io.LimitReader(reader, maxBytes+1)
	data, err := io.ReadAll(limited)
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > maxBytes {
		return nil, errDecodedBodyTooLarge
	}
	return data, nil
}

func isProtobufContentType(contentType string) bool {
	normalized := strings.ToLower(strings.TrimSpace(contentType))
	return strings.Contains(normalized, "protobuf") || strings.Contains(normalized, "application/x-protobuf")
}

func looksLikeGzip(body []byte) bool {
	return len(body) >= 2 && body[0] == 0x1f && body[1] == 0x8b
}

func tryDecodeGzipByMagic(body []byte, maxDecodedBytes int64) ([]byte, bool, error) {
	if !looksLikeGzip(body) {
		return body, false, nil
	}
	reader, err := gzip.NewReader(bytes.NewReader(body))
	if err != nil {
		return body, false, nil
	}
	decoded, readErr := readAllWithLimit(reader, maxDecodedBytes)
	_ = reader.Close()
	if readErr != nil {
		if errors.Is(readErr, errDecodedBodyTooLarge) {
			return nil, false, errDecodedBodyTooLarge
		}
		return body, false, nil
	}
	return decoded, true, nil
}

func parsePayloadToString(ctx context.Context, body []byte, dataType string, protobufEnabled bool, maxDecodedBytes int64) (string, any, string, bool, error) {
	autoGzipMagic := false

	if protobufEnabled {
		if parsed, err := parseProtobufPayload(dataType, body); err == nil {
			encoded, _ := json.Marshal(parsed)
			return string(encoded), parsed, "protobuf", autoGzipMagic, nil
		}

		retryBody, decompressed, decompressErr := tryDecodeGzipByMagic(body, maxDecodedBytes)
		if decompressErr != nil {
			return "", nil, "", autoGzipMagic, decompressErr
		}
		if decompressed {
			autoGzipMagic = true
			if parsed, err := parseProtobufPayload(dataType, retryBody); err == nil {
				encoded, _ := json.Marshal(parsed)
				return string(encoded), parsed, "protobuf", autoGzipMagic, nil
			}
			body = retryBody
		}

		var fallback any
		if err := json.Unmarshal(body, &fallback); err == nil {
			encoded, _ := json.Marshal(fallback)
			return string(encoded), fallback, "json", autoGzipMagic, nil
		}

		return base64.StdEncoding.EncodeToString(body), nil, "binary", autoGzipMagic, nil
	}

	var parsed any
	if err := json.Unmarshal(body, &parsed); err == nil {
		encoded, _ := json.Marshal(parsed)
		return string(encoded), parsed, "json", autoGzipMagic, nil
	}

	retryBody, decompressed, decompressErr := tryDecodeGzipByMagic(body, maxDecodedBytes)
	if decompressErr != nil {
		return "", nil, "", autoGzipMagic, decompressErr
	}
	if decompressed {
		autoGzipMagic = true
		var retriedParsed any
		if err := json.Unmarshal(retryBody, &retriedParsed); err == nil {
			encoded, _ := json.Marshal(retriedParsed)
			return string(encoded), retriedParsed, "json", autoGzipMagic, nil
		}
		body = retryBody
	}

	_ = ctx
	return base64.StdEncoding.EncodeToString(body), nil, "binary", autoGzipMagic, nil
}

func parseProtobufPayload(dataType string, body []byte) (any, error) {
	marshalOptions := protojson.MarshalOptions{UseProtoNames: false, EmitUnpopulated: false}

	switch dataType {
	case "logs":
		request := &collectorlogsv1.ExportLogsServiceRequest{}
		if err := proto.Unmarshal(body, request); err != nil {
			return nil, err
		}
		return marshalProtoJSON(marshalOptions, request)
	case "metrics":
		request := &collectormetricsv1.ExportMetricsServiceRequest{}
		if err := proto.Unmarshal(body, request); err != nil {
			return nil, err
		}
		return marshalProtoJSON(marshalOptions, request)
	case "traces":
		request := &collectortracev1.ExportTraceServiceRequest{}
		if err := proto.Unmarshal(body, request); err != nil {
			return nil, err
		}
		return marshalProtoJSON(marshalOptions, request)
	default:
		return nil, fmt.Errorf("unknown data type: %s", dataType)
	}
}

func marshalProtoJSON(options protojson.MarshalOptions, message proto.Message) (any, error) {
	encoded, err := options.Marshal(message)
	if err != nil {
		return nil, err
	}
	var parsed any
	if err := json.Unmarshal(encoded, &parsed); err != nil {
		return nil, err
	}
	return parsed, nil
}
