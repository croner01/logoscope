package ingest

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strings"
	"time"
)

// Server 封装 HTTP 路由与依赖。
type Server struct {
	cfg   Config
	queue *QueueWriter
}

func NewServer(cfg Config, queue *QueueWriter) *Server {
	return &Server{cfg: cfg, queue: queue}
}

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/logs", s.ingestHandler("logs"))
	mux.HandleFunc("/v1/metrics", s.ingestHandler("metrics"))
	mux.HandleFunc("/v1/traces", s.ingestHandler("traces"))
	mux.HandleFunc("/health", s.healthHandler)
	mux.HandleFunc("/ready", s.readyHandler)
	mux.HandleFunc("/api/v1/queue/stats", s.queueStatsHandler)
	mux.HandleFunc("/metrics", s.metricsHandler)
	mux.HandleFunc("/", s.rootHandler)
	return loggingMiddleware(mux)
}

func (s *Server) ingestHandler(dataType string) http.HandlerFunc {
	return func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost {
			writeJSONError(writer, http.StatusMethodNotAllowed, "method not allowed")
			return
		}

		bodyReader := request.Body
		if s.cfg.MaxRequestBodyBytes > 0 {
			bodyReader = http.MaxBytesReader(writer, request.Body, s.cfg.MaxRequestBodyBytes)
		}
		rawBody, err := io.ReadAll(bodyReader)
		if err != nil {
			var maxBytesErr *http.MaxBytesError
			if errors.As(err, &maxBytesErr) {
				writeJSONError(writer, http.StatusRequestEntityTooLarge, "request body too large")
				return
			}
			writeJSONError(writer, http.StatusBadRequest, "invalid request body")
			return
		}
		if len(rawBody) == 0 {
			writeJSONError(writer, http.StatusBadRequest, "Empty request body")
			return
		}

		decodedBody, err := decodeBodyByContentEncoding(request, rawBody, s.cfg.MaxRequestBodyBytes)
		if err != nil {
			if errors.Is(err, errDecodedBodyTooLarge) {
				writeJSONError(writer, http.StatusRequestEntityTooLarge, "decoded request body too large")
				return
			}
			if errors.Is(err, errUnsupportedContentEncoding) {
				writeJSONError(writer, http.StatusUnsupportedMediaType, err.Error())
				return
			}
			writeJSONError(writer, http.StatusBadRequest, err.Error())
			return
		}

		contentType := request.Header.Get("Content-Type")
		isProtobuf := isProtobufContentType(contentType)

		payloadText, parsedPayload, parsedFormat, autoGzipMagic, parseErr := parsePayloadToString(
			request.Context(),
			decodedBody,
			dataType,
			isProtobuf,
			s.cfg.MaxRequestBodyBytes,
		)
		if parseErr != nil {
			if errors.Is(parseErr, errDecodedBodyTooLarge) {
				writeJSONError(writer, http.StatusRequestEntityTooLarge, "decoded request body too large")
				return
			}
			writeJSONError(writer, http.StatusBadRequest, "invalid request body")
			return
		}

		metadata := map[string]any{
			"content_type":       contentType,
			"content_encoding":   request.Header.Get("Content-Encoding"),
			"raw_content_length": len(rawBody),
			"content_length":     len(decodedBody),
			"is_binary":          parsedFormat == "binary",
			"is_protobuf":        isProtobuf,
			"protobuf_parsed":    parsedFormat == "protobuf",
			"parsed_format":      parsedFormat,
			"auto_gzip_magic":    autoGzipMagic,
			"_parsed_payload":    parsedPayload,
		}

		stream := s.getStreamName(dataType)
		_, err = s.queue.WriteToQueue(request.Context(), stream, dataType, payloadText, metadata)
		if err != nil {
			var backpressureErr *QueueBackpressureError
			if errors.As(err, &backpressureErr) {
				writeJSONError(writer, http.StatusServiceUnavailable, "Ingest queue is full, please retry")
				return
			}
			log.Printf("[ingest-go] write queue failed: type=%s err=%v", dataType, err)
			writeJSONError(writer, http.StatusInternalServerError, "Internal server error")
			return
		}

		message := "Ingested successfully"
		switch dataType {
		case "logs":
			message = "Logs ingested successfully"
		case "metrics":
			message = "Metrics ingested successfully"
		case "traces":
			message = "Traces ingested successfully"
		}

		writeJSON(writer, http.StatusOK, map[string]any{
			"status":  "success",
			"message": message,
			"service": s.cfg.AppName,
			"format":  parsedFormat,
		})
	}
}

func (s *Server) healthHandler(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		writeJSONError(writer, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	stats := s.queue.GetStats()
	queueConnected := asBool(stats["queue_connected"])
	queueBackend := asString(stats["queue_backend"])

	writeJSON(writer, http.StatusOK, map[string]any{
		"status":          "healthy",
		"service":         s.cfg.AppName,
		"version":         s.cfg.AppVersion,
		"mode":            stats["mode"],
		"queue_backend":   queueBackend,
		"queue_connected": queueConnected,
		"kafka_connected": stats["kafka_connected"],
		"memory_queue": map[string]any{
			"size":           stats["memory_queue_size"],
			"max_size":       stats["memory_queue_max_size"],
			"fill_ratio":     stats["memory_queue_fill_ratio"],
			"high_watermark": stats["memory_queue_high_watermark"],
			"dropped":        stats["dropped"],
		},
		"backpressure": map[string]any{
			"backpressure_rejected": stats["backpressure_rejected"],
		},
		"stats": map[string]any{
			"total_written":               stats["total_written"],
			"queue_written":               stats["queue_written"],
			"kafka_written":               stats["kafka_written"],
			"memory_queued":               stats["memory_queued"],
			"memory_queue_flushed":        stats["memory_queue_flushed"],
			"memory_queue_flush_failures": stats["memory_queue_flush_failures"],
			"reconnect_attempts":          stats["reconnect_attempts"],
			"wal_appended":                stats["wal_appended"],
			"wal_acked":                   stats["wal_acked"],
			"wal_replay_recovered":        stats["wal_replay_recovered"],
			"wal_write_failures":          stats["wal_write_failures"],
		},
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano),
	})
}

func (s *Server) readyHandler(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		writeJSONError(writer, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writeJSON(writer, http.StatusOK, map[string]any{
		"ready":     true,
		"service":   s.cfg.AppName,
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano),
	})
}

func (s *Server) queueStatsHandler(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		writeJSONError(writer, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writeJSON(writer, http.StatusOK, map[string]any{
		"status":    "ok",
		"service":   s.cfg.AppName,
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano),
		"queue":     s.queue.GetStats(),
	})
}

func (s *Server) metricsHandler(writer http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		writeJSONError(writer, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	writer.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	_, _ = writer.Write([]byte(s.queue.RenderPrometheusMetrics()))
}

func (s *Server) rootHandler(writer http.ResponseWriter, request *http.Request) {
	if request.URL.Path != "/" {
		writeJSONError(writer, http.StatusNotFound, "not found")
		return
	}
	if request.Method != http.MethodGet {
		writeJSONError(writer, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	stats := s.queue.GetStats()
	writeJSON(writer, http.StatusOK, map[string]any{
		"service":       "Ingest Service",
		"version":       s.cfg.AppVersion,
		"description":   "Logoscope OTLP 数据摄入服务",
		"mode":          stats["mode"],
		"queue_backend": stats["queue_backend"],
		"features": map[string]any{
			"lazy_queue_connection": true,
			"memory_queue_fallback": true,
			"auto_reconnect":        true,
			"wal":                   stats["wal_enabled"],
		},
	})
}

func (s *Server) getStreamName(dataType string) string {
	switch dataType {
	case "logs":
		return s.cfg.KafkaTopicLogs
	case "metrics":
		return s.cfg.KafkaTopicMetrics
	case "traces":
		return s.cfg.KafkaTopicTraces
	default:
		return s.cfg.KafkaTopicDefault
	}
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		start := time.Now()
		next.ServeHTTP(writer, request)
		duration := time.Since(start)
		if strings.HasPrefix(request.URL.Path, "/health") || strings.HasPrefix(request.URL.Path, "/ready") {
			return
		}
		log.Printf("[ingest-go] %s %s cost=%s", request.Method, request.URL.Path, duration)
	})
}

func writeJSON(writer http.ResponseWriter, statusCode int, payload map[string]any) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(statusCode)
	encoder := json.NewEncoder(writer)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(payload)
}

func writeJSONError(writer http.ResponseWriter, statusCode int, detail string) {
	writeJSON(writer, statusCode, map[string]any{"detail": detail})
}

func Run(ctx context.Context, cfg Config) error {
	queue := NewQueueWriter(cfg)
	if err := queue.Init(); err != nil {
		return err
	}

	server := NewServer(cfg, queue)
	httpServer := &http.Server{
		Addr:              cfg.Addr(),
		Handler:           server.Routes(),
		ReadHeaderTimeout: cfg.ReadHeaderTimeout(),
		ReadTimeout:       cfg.ReadTimeout(),
		WriteTimeout:      cfg.WriteTimeout(),
		IdleTimeout:       cfg.IdleTimeout(),
	}

	errCh := make(chan error, 1)
	go func() {
		log.Printf("[ingest-go] server starting at %s", cfg.Addr())
		err := httpServer.ListenAndServe()
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
			return
		}
		errCh <- nil
	}()

	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout())
		defer cancel()
		_ = httpServer.Shutdown(shutdownCtx)
		queue.Shutdown(shutdownCtx)
		return nil
	case err := <-errCh:
		shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout())
		defer cancel()
		queue.Shutdown(shutdownCtx)
		return err
	}
}
