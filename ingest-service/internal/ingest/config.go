package ingest

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config 定义 ingest-service Go 版本运行配置。
type Config struct {
	AppName              string
	AppVersion           string
	Host                 string
	Port                 int
	ShutdownTimeoutSec   int
	ReadHeaderTimeoutSec int
	ReadTimeoutSec       int
	WriteTimeoutSec      int
	IdleTimeoutSec       int
	MaxRequestBodyBytes  int64
	QueueBackend         string
	QueueReconnectSec    int
	QueueFlushIntervalMs int

	KafkaTopicDefault string
	KafkaTopicLogs    string
	KafkaTopicMetrics string
	KafkaTopicTraces  string

	KafkaBrokers              []string
	KafkaDialTimeout          int
	KafkaWriteTimeout         int
	KafkaRequiredAcks         int
	KafkaWriteMode            string
	KafkaPingIntervalSec      float64
	KafkaReconnectIntervalSec int
	KafkaMaxReconnectAttempts int
	KafkaBatchSize            int
	KafkaBatchBytes           int
	KafkaBatchTimeoutMs       int

	QueueLogRecordBatchSize   int
	MemoryQueueMaxSize        int
	MemoryQueueFlushBatchSize int
	MemoryQueueDropOldest     bool
	WALEnabled                bool
	WALDir                    string
	WALFileName               string
	WALSyncEvery              int

	LogLevel string
}

func LoadConfig() Config {
	kafkaTopicDefault := getEnv("KAFKA_TOPIC", "logs.raw")

	cfg := Config{
		AppName:              getEnv("APP_NAME", "ingest-service"),
		AppVersion:           getEnv("APP_VERSION", "1.0.0"),
		Host:                 getEnv("HOST", "0.0.0.0"),
		Port:                 maxInt(1, getEnvInt("PORT", 8080)),
		ShutdownTimeoutSec:   maxInt(5, getEnvInt("SHUTDOWN_TIMEOUT_SEC", 120)),
		ReadHeaderTimeoutSec: maxInt(1, getEnvInt("READ_HEADER_TIMEOUT_SEC", 5)),
		ReadTimeoutSec:       maxInt(1, getEnvInt("READ_TIMEOUT_SEC", 30)),
		WriteTimeoutSec:      maxInt(1, getEnvInt("WRITE_TIMEOUT_SEC", 30)),
		IdleTimeoutSec:       maxInt(1, getEnvInt("IDLE_TIMEOUT_SEC", 120)),
		MaxRequestBodyBytes:  maxInt64(1024, getEnvInt64("MAX_REQUEST_BODY_BYTES", 10*1024*1024)),
		QueueBackend:         normalizeQueueBackend(getEnv("QUEUE_BACKEND", "kafka")),
		QueueReconnectSec:    maxInt(1, getEnvInt("QUEUE_RECONNECT_INTERVAL_SEC", 5)),
		QueueFlushIntervalMs: maxInt(10, getEnvInt("QUEUE_FLUSH_INTERVAL_MS", 100)),

		KafkaTopicDefault: kafkaTopicDefault,
		KafkaTopicLogs:    getEnv("KAFKA_TOPIC_LOGS", kafkaTopicDefault),
		KafkaTopicMetrics: getEnv("KAFKA_TOPIC_METRICS", "metrics.raw"),
		KafkaTopicTraces:  getEnv("KAFKA_TOPIC_TRACES", "traces.raw"),

		KafkaBrokers:              parseCSV(getEnv("KAFKA_BROKERS", "kafka:9092")),
		KafkaDialTimeout:          maxInt(1, getEnvInt("KAFKA_DIAL_TIMEOUT_SEC", 3)),
		KafkaWriteTimeout:         maxInt(1, getEnvInt("KAFKA_WRITE_TIMEOUT_SEC", 5)),
		KafkaRequiredAcks:         parseKafkaRequiredAcks(getEnv("KAFKA_REQUIRED_ACKS", "leader")),
		KafkaWriteMode:            normalizeKafkaWriteMode(getEnv("KAFKA_WRITE_MODE", "sync")),
		KafkaPingIntervalSec:      maxFloat(1.0, getEnvFloat("KAFKA_PING_INTERVAL_SEC", 5.0)),
		KafkaReconnectIntervalSec: maxInt(1, getEnvInt("KAFKA_RECONNECT_INTERVAL_SEC", 5)),
		KafkaMaxReconnectAttempts: maxInt(1, getEnvInt("KAFKA_MAX_RECONNECT_ATTEMPTS", 3)),
		KafkaBatchSize:            maxInt(1, getEnvInt("KAFKA_BATCH_SIZE", 500)),
		KafkaBatchBytes:           maxInt(1024, getEnvInt("KAFKA_BATCH_BYTES", 1048576)),
		KafkaBatchTimeoutMs:       maxInt(1, getEnvInt("KAFKA_BATCH_TIMEOUT_MS", 20)),

		QueueLogRecordBatchSize:   maxInt(1, getEnvInt("QUEUE_LOG_RECORD_BATCH_SIZE", 200)),
		MemoryQueueMaxSize:        maxInt(1, getEnvInt("MEMORY_QUEUE_MAX_SIZE", 1000)),
		MemoryQueueFlushBatchSize: maxInt(1, getEnvInt("MEMORY_QUEUE_FLUSH_BATCH_SIZE", 200)),
		MemoryQueueDropOldest:     getEnvBool("MEMORY_QUEUE_DROP_OLDEST_WHEN_FULL", false),
		WALEnabled:                getEnvBool("WAL_ENABLED", true),
		WALDir:                    getEnv("WAL_DIR", "/var/lib/ingest/wal"),
		WALFileName:               getEnv("WAL_FILE_NAME", "queue.wal"),
		WALSyncEvery:              maxInt(1, getEnvInt("WAL_SYNC_EVERY", 1)),

		LogLevel: strings.ToLower(getEnv("LOG_LEVEL", "info")),
	}

	return cfg
}

func (c Config) Addr() string {
	return fmt.Sprintf("%s:%d", c.Host, c.Port)
}

func (c Config) ShutdownTimeout() time.Duration {
	return time.Duration(c.ShutdownTimeoutSec) * time.Second
}

func (c Config) ReadHeaderTimeout() time.Duration {
	return time.Duration(c.ReadHeaderTimeoutSec) * time.Second
}

func (c Config) ReadTimeout() time.Duration {
	return time.Duration(c.ReadTimeoutSec) * time.Second
}

func (c Config) WriteTimeout() time.Duration {
	return time.Duration(c.WriteTimeoutSec) * time.Second
}

func (c Config) IdleTimeout() time.Duration {
	return time.Duration(c.IdleTimeoutSec) * time.Second
}

func (c Config) QueueFlushInterval() time.Duration {
	return time.Duration(c.QueueFlushIntervalMs) * time.Millisecond
}

func (c Config) KafkaPingInterval() time.Duration {
	return time.Duration(c.KafkaPingIntervalSec * float64(time.Second))
}

func (c Config) KafkaReconnectInterval() time.Duration {
	return time.Duration(c.KafkaReconnectIntervalSec) * time.Second
}

func (c Config) KafkaAsyncEnabled() bool {
	return c.QueueBackend == "kafka" && c.KafkaWriteMode == "async"
}

func getEnv(key string, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func getEnvInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func getEnvInt64(key string, fallback int64) int64 {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return fallback
	}
	return parsed
}

func getEnvFloat(key string, fallback float64) float64 {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil {
		return fallback
	}
	return parsed
}

func getEnvBool(key string, fallback bool) bool {
	value := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	if value == "" {
		return fallback
	}
	if value == "1" || value == "true" || value == "yes" || value == "on" {
		return true
	}
	if value == "0" || value == "false" || value == "no" || value == "off" {
		return false
	}
	return fallback
}

func maxInt(a int, b int) int {
	if a > b {
		return a
	}
	return b
}

func maxInt64(a int64, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func maxFloat(a float64, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

func parseCSV(raw string) []string {
	items := make([]string, 0, 4)
	for _, part := range strings.Split(raw, ",") {
		value := strings.TrimSpace(part)
		if value == "" {
			continue
		}
		items = append(items, value)
	}
	if len(items) == 0 {
		return []string{"kafka:9092"}
	}
	return items
}

func normalizeQueueBackend(raw string) string {
	_ = strings.ToLower(strings.TrimSpace(raw))
	return "kafka"
}

func parseKafkaRequiredAcks(raw string) int {
	value := strings.ToLower(strings.TrimSpace(raw))
	switch value {
	case "none", "0":
		return 0
	case "all", "-1":
		return -1
	default:
		return 1
	}
}

func normalizeKafkaWriteMode(raw string) string {
	value := strings.ToLower(strings.TrimSpace(raw))
	switch value {
	case "async":
		return "async"
	default:
		return "sync"
	}
}
