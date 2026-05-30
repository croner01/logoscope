package ingest

import (
	"bytes"
	"compress/gzip"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestIngestHandlerRejectsTooLargeBody(t *testing.T) {
	server := NewServer(
		Config{
			AppName:             "ingest-service",
			MaxRequestBodyBytes: 8,
			MemoryQueueMaxSize:  16,
		},
		NewQueueWriter(Config{MemoryQueueMaxSize: 16}),
	)

	request := httptest.NewRequest(http.MethodPost, "/v1/logs", strings.NewReader(strings.Repeat("a", 32)))
	response := httptest.NewRecorder()

	server.Routes().ServeHTTP(response, request)

	if response.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected status 413, got %d body=%s", response.Code, response.Body.String())
	}
	if !strings.Contains(response.Body.String(), "request body too large") {
		t.Fatalf("expected request body too large error, got %s", response.Body.String())
	}
}

func TestIngestHandlerRejectsTooLargeDecodedGzipBody(t *testing.T) {
	server := NewServer(
		Config{
			AppName:             "ingest-service",
			MaxRequestBodyBytes: 64,
			MemoryQueueMaxSize:  16,
		},
		NewQueueWriter(Config{MemoryQueueMaxSize: 16}),
	)

	decodedPayload := strings.Repeat("a", 1024)
	var compressed bytes.Buffer
	gzipWriter := gzip.NewWriter(&compressed)
	if _, err := gzipWriter.Write([]byte(decodedPayload)); err != nil {
		t.Fatalf("gzip write failed: %v", err)
	}
	if err := gzipWriter.Close(); err != nil {
		t.Fatalf("gzip close failed: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/v1/logs", bytes.NewReader(compressed.Bytes()))
	request.Header.Set("Content-Encoding", "gzip")
	response := httptest.NewRecorder()

	server.Routes().ServeHTTP(response, request)

	if response.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected status 413, got %d body=%s", response.Code, response.Body.String())
	}
	if !strings.Contains(response.Body.String(), "decoded request body too large") {
		t.Fatalf("expected decoded size rejection, got %s", response.Body.String())
	}
}

func TestIngestHandlerRejectsTooLargeAutoGzipMagicBody(t *testing.T) {
	server := NewServer(
		Config{
			AppName:             "ingest-service",
			MaxRequestBodyBytes: 64,
			MemoryQueueMaxSize:  16,
		},
		NewQueueWriter(Config{MemoryQueueMaxSize: 16}),
	)

	decodedPayload := strings.Repeat("a", 1024)
	var compressed bytes.Buffer
	gzipWriter := gzip.NewWriter(&compressed)
	if _, err := gzipWriter.Write([]byte(decodedPayload)); err != nil {
		t.Fatalf("gzip write failed: %v", err)
	}
	if err := gzipWriter.Close(); err != nil {
		t.Fatalf("gzip close failed: %v", err)
	}

	request := httptest.NewRequest(http.MethodPost, "/v1/logs", bytes.NewReader(compressed.Bytes()))
	response := httptest.NewRecorder()

	server.Routes().ServeHTTP(response, request)

	if response.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected status 413, got %d body=%s", response.Code, response.Body.String())
	}
	if !strings.Contains(response.Body.String(), "decoded request body too large") {
		t.Fatalf("expected decoded size rejection, got %s", response.Body.String())
	}
}
