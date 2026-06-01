package ingest

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const (
	uploadBatchSize     = 100
	uploadScanMaxLines  = 50
	uploadMaxBodyBytes  = 500 * 1024 * 1024 // 500 MB
	uploadMaxLineLength = 1 * 1024 * 1024   // 1 MB per line
)

var (
	uploadTimestampPatterns = []*regexp.Regexp{
		regexp.MustCompile(`(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))`),
		regexp.MustCompile(`(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+\d+\s+(ERROR|CRITICAL|WARN|INFO|DEBUG)`),
		regexp.MustCompile(`(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+(?:\[)?(ERROR|CRITICAL|WARN|INFO|DEBUG)`),
		regexp.MustCompile(`(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)`),
	}
	uploadServiceNamePattern = regexp.MustCompile(`\d+\s+(?:ERROR|CRITICAL|WARN|INFO|DEBUG)\s+([\w-]+)`)

	genericLogNames = map[string]bool{
		"output": true, "log": true, "console": true,
		"messages": true, "stdout": true, "stderr": true, "syslog": true,
	}
)

type uploadRecord struct {
	Message   string `json:"message"`
	Timestamp string `json:"timestamp,omitempty"`
	Level     string `json:"level"`
}

func (s *Server) uploadHandler(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodPost {
		writeJSONError(writer, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	request.Body = http.MaxBytesReader(writer, request.Body, uploadMaxBodyBytes)

	if err := request.ParseMultipartForm(32 << 20); err != nil {
		writeJSONError(writer, http.StatusBadRequest, "Failed to parse upload: "+err.Error())
		return
	}
	defer request.MultipartForm.RemoveAll()

	fileHeaders := request.MultipartForm.File["file"]
	if len(fileHeaders) == 0 {
		writeJSONError(writer, http.StatusBadRequest, "No file provided")
		return
	}
	fileHeader := fileHeaders[0]
	file, err := fileHeader.Open()
	if err != nil {
		writeJSONError(writer, http.StatusBadRequest, "Failed to open file: "+err.Error())
		return
	}
	defer file.Close()

	serviceName := strings.TrimSpace(request.FormValue("service_name"))
	namespace := strings.TrimSpace(request.FormValue("namespace"))
	if namespace == "" {
		namespace = "default"
	}

	uploadID := fmt.Sprintf("upl_%x", time.Now().UnixNano())
	filename := fileHeader.Filename
	ext := strings.ToLower(filepath.Ext(filename))

	log.Printf("[upload] start: upload_id=%s filename=%s service_input=%q ext=%s",
		uploadID, filename, serviceName, ext)

	br := bufio.NewReaderSize(file, 64*1024)
	total := 0
	batches := 0
	resolvedService := serviceName
	var firstLines []string
	batch := make([]map[string]any, 0, uploadBatchSize)

	flushBatch := func() error {
		if len(batch) == 0 {
			return nil
		}
		envelope := buildUploadEnvelope(uploadID, resolvedService, namespace, batches, batch)
		envelopeJSON, err := json.Marshal(envelope)
		if err != nil {
			return fmt.Errorf("marshal envelope: %w", err)
		}
		_, err = s.queue.WriteToQueue(
			request.Context(),
			s.cfg.KafkaTopicLogs,
			"logs",
			string(envelopeJSON),
			map[string]any{
				"upload_id":  uploadID,
				"batch_index": batches,
				"source":     "upload",
				"service_name": resolvedService,
			},
		)
		if err != nil {
			return fmt.Errorf("write batch to queue: %w", err)
		}
		total += len(batch)
		batches++
		batch = make([]map[string]any, 0, uploadBatchSize)
		return nil
	}

	// Determine format by peeking first non-whitespace bytes
	peek, peekErr := br.Peek(4096)

	switch {
	case ext == ".json" && (peekErr == nil || errors.Is(peekErr, io.EOF)) && isJSONArray(peek):
		// JSON array — use json.Decoder streaming
		decoder := json.NewDecoder(br)
		if err := readJSONArrayToken(decoder); err != nil {
			writeJSONError(writer, http.StatusBadRequest, "Invalid JSON array: "+err.Error())
			return
		}
		for decoder.More() {
			var raw map[string]any
			if err := decoder.Decode(&raw); err != nil {
				continue // skip malformed items
			}
			rec := normalizeJSONRecord(raw)

			if len(firstLines) < uploadScanMaxLines {
				if msg, _ := rec["message"].(string); msg != "" {
					firstLines = append(firstLines, msg)
				}
			}
			if resolvedService == "" && len(firstLines) > 0 {
				resolvedService = resolveUploadServiceName(filename, firstLines, serviceName)
			}

			batch = append(batch, rec)
			if len(batch) >= uploadBatchSize {
				if err := flushBatch(); err != nil {
					log.Printf("[upload] flush error: %v", err)
					writeJSONError(writer, http.StatusInternalServerError, "Write failed")
					return
				}
			}
		}

	default:
		// NDJSON or text log — line-by-line scanning
		scanner := bufio.NewScanner(br)
		scanner.Buffer(make([]byte, 0, 64*1024), uploadMaxLineLength)

		for scanner.Scan() {
			line := scanner.Text()
			if strings.TrimSpace(line) == "" {
				continue
			}

			var rec map[string]any
			if ext == ".json" || ext == ".ndjson" {
				rec = parseNDJSONRecord(line)
			} else {
				rec = parseTextRecord(line)
			}
			if rec == nil {
				continue
			}

			if len(firstLines) < uploadScanMaxLines {
				if msg, _ := rec["message"].(string); msg != "" {
					firstLines = append(firstLines, msg)
				}
			}
			if resolvedService == "" && len(firstLines) > 0 {
				resolvedService = resolveUploadServiceName(filename, firstLines, serviceName)
			}

			batch = append(batch, rec)
			if len(batch) >= uploadBatchSize {
				if err := flushBatch(); err != nil {
					log.Printf("[upload] flush error: %v", err)
					writeJSONError(writer, http.StatusInternalServerError, "Write failed")
					return
				}
			}
		}
		if err := scanner.Err(); err != nil {
			log.Printf("[upload] scan error: %v", err)
		}
	}

	// Flush remaining
	if len(batch) > 0 {
		if resolvedService == "" {
			resolvedService = resolveUploadServiceName(filename, firstLines, serviceName)
		}
		if err := flushBatch(); err != nil {
			log.Printf("[upload] final flush error: %v", err)
			writeJSONError(writer, http.StatusInternalServerError, "Write failed")
			return
		}
	}

	log.Printf("[upload] complete: upload_id=%s filename=%s service=%s total=%d batches=%d",
		uploadID, filename, resolvedService, total, batches)

	writeJSON(writer, http.StatusOK, map[string]any{
		"status":    "accepted",
		"upload_id": uploadID,
		"total":     total,
		"batches":   batches,
	})
}

// --- helpers ---

func isJSONArray(peek []byte) bool {
	trimmed := strings.TrimSpace(string(peek))
	return strings.HasPrefix(trimmed, "[")
}

func readJSONArrayToken(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delim, ok := token.(json.Delim)
	if !ok || delim != json.Delim('[') {
		return fmt.Errorf("expected JSON array start '['")
	}
	return nil
}

func parseNDJSONRecord(line string) map[string]any {
	trimmed := strings.TrimSpace(line)
	if trimmed == "" {
		return nil
	}
	var raw map[string]any
	if err := json.Unmarshal([]byte(trimmed), &raw); err != nil {
		return nil
	}
	return normalizeJSONRecord(raw)
}

func parseTextRecord(line string) map[string]any {
	msg := strings.TrimRight(line, "\r\n")
	if msg == "" {
		return nil
	}
	rec := map[string]any{
		"message": msg,
		"level":   "INFO",
	}
	for i, pattern := range uploadTimestampPatterns {
		matches := pattern.FindStringSubmatch(msg)
		if len(matches) >= 2 {
			rec["timestamp"] = matches[1]
			if i >= 1 && i <= 2 && len(matches) >= 3 {
				level := strings.ToUpper(matches[2])
				rec["level"] = level
			}
			break
		}
	}
	return rec
}

func normalizeJSONRecord(rec map[string]any) map[string]any {
	if rec == nil {
		rec = make(map[string]any)
	}
	// Normalize message field
	if _, ok := rec["message"]; !ok {
		for _, key := range []string{"log", "msg", "text", "body"} {
			if val, exists := rec[key]; exists {
				if str, ok := val.(string); ok && str != "" {
					rec["message"] = str
					break
				}
			}
		}
	}
	// Normalize timestamp field
	if _, ok := rec["timestamp"]; !ok {
		for _, key := range []string{"@timestamp", "time", "ts", "datetime"} {
			if val, exists := rec[key]; exists {
				rec["timestamp"] = fmt.Sprintf("%v", val)
				break
			}
		}
	}
	// Normalize level field
	if _, ok := rec["level"]; !ok {
		for _, key := range []string{"log_level", "severity", "severity_text"} {
			if val, exists := rec[key]; exists {
				rec["level"] = strings.ToUpper(fmt.Sprintf("%v", val))
				break
			}
		}
	}
	return rec
}

func buildUploadEnvelope(uploadID, serviceName, namespace string, batchIndex int, records []map[string]any) map[string]any {
	streamRecords := make([]map[string]any, 0, len(records))
	for i, rec := range records {
		msg, _ := rec["message"].(string)
		ts, _ := rec["timestamp"].(string)
		level, _ := rec["level"].(string)
		if level == "" {
			level = "INFO"
		}
		streamRecords = append(streamRecords, map[string]any{
			"message":   msg,
			"timestamp": ts,
			"level":     level,
			"service_name": serviceName,
			"_raw_attributes": map[string]any{
				"upload_id":     uploadID,
				"batch_index":   batchIndex,
				"record_index":  i,
				"source":        "upload",
			},
		})
	}

	return map[string]any{
		"type":         "upload",
		"upload_id":    uploadID,
		"service_name": serviceName,
		"namespace":    namespace,
		"records":      streamRecords,
	}
}

func resolveUploadServiceName(filename string, firstLines []string, userInput string) string {
	if userInput != "" {
		return userInput
	}
	for _, line := range firstLines {
		matches := uploadServiceNamePattern.FindStringSubmatch(line)
		if len(matches) >= 2 {
			return matches[1]
		}
	}
	stem := strings.TrimSuffix(filename, filepath.Ext(filename))
	if !genericLogNames[strings.ToLower(stem)] {
		return stem
	}
	return "offline-upload"
}
