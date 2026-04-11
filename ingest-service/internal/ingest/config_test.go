package ingest

import "testing"

func TestLoadConfigKafkaReconnectSettings(t *testing.T) {
	t.Setenv("KAFKA_PING_INTERVAL_SEC", "9")
	t.Setenv("KAFKA_RECONNECT_INTERVAL_SEC", "7")
	t.Setenv("KAFKA_MAX_RECONNECT_ATTEMPTS", "11")

	cfg := LoadConfig()

	if cfg.KafkaPingIntervalSec != 9 {
		t.Fatalf("expected kafka ping interval 9, got %v", cfg.KafkaPingIntervalSec)
	}
	if cfg.KafkaReconnectIntervalSec != 7 {
		t.Fatalf("expected kafka reconnect interval 7, got %d", cfg.KafkaReconnectIntervalSec)
	}
	if cfg.KafkaMaxReconnectAttempts != 11 {
		t.Fatalf("expected kafka max reconnect attempts 11, got %d", cfg.KafkaMaxReconnectAttempts)
	}
}

func TestLoadConfigQueueBackendAlwaysKafka(t *testing.T) {
	t.Setenv("QUEUE_BACKEND", "redis")

	cfg := LoadConfig()

	if cfg.QueueBackend != "kafka" {
		t.Fatalf("expected queue backend kafka, got %s", cfg.QueueBackend)
	}
}

func TestLoadConfigKafkaReconnectFallsBackToDefaults(t *testing.T) {
	t.Setenv("KAFKA_PING_INTERVAL_SEC", "")
	t.Setenv("KAFKA_RECONNECT_INTERVAL_SEC", "")
	t.Setenv("KAFKA_MAX_RECONNECT_ATTEMPTS", "")

	cfg := LoadConfig()

	if cfg.KafkaPingIntervalSec != 5 {
		t.Fatalf("expected kafka ping interval fallback 5, got %v", cfg.KafkaPingIntervalSec)
	}
	if cfg.KafkaReconnectIntervalSec != 5 {
		t.Fatalf("expected kafka reconnect interval fallback 5, got %d", cfg.KafkaReconnectIntervalSec)
	}
	if cfg.KafkaMaxReconnectAttempts != 3 {
		t.Fatalf("expected kafka max reconnect attempts fallback 3, got %d", cfg.KafkaMaxReconnectAttempts)
	}
}

func TestLoadConfigMaxRequestBodyBytes(t *testing.T) {
	t.Setenv("MAX_REQUEST_BODY_BYTES", "2048")

	cfg := LoadConfig()
	if cfg.MaxRequestBodyBytes != 2048 {
		t.Fatalf("expected max request body bytes 2048, got %d", cfg.MaxRequestBodyBytes)
	}
}

func TestLoadConfigMaxRequestBodyBytesHasLowerBound(t *testing.T) {
	t.Setenv("MAX_REQUEST_BODY_BYTES", "16")

	cfg := LoadConfig()
	if cfg.MaxRequestBodyBytes != 1024 {
		t.Fatalf("expected max request body bytes lower bound 1024, got %d", cfg.MaxRequestBodyBytes)
	}
}

func TestLoadConfigHTTPServerTimeouts(t *testing.T) {
	t.Setenv("READ_HEADER_TIMEOUT_SEC", "3")
	t.Setenv("READ_TIMEOUT_SEC", "15")
	t.Setenv("WRITE_TIMEOUT_SEC", "20")
	t.Setenv("IDLE_TIMEOUT_SEC", "45")

	cfg := LoadConfig()

	if cfg.ReadHeaderTimeoutSec != 3 {
		t.Fatalf("expected read header timeout 3, got %d", cfg.ReadHeaderTimeoutSec)
	}
	if cfg.ReadTimeoutSec != 15 {
		t.Fatalf("expected read timeout 15, got %d", cfg.ReadTimeoutSec)
	}
	if cfg.WriteTimeoutSec != 20 {
		t.Fatalf("expected write timeout 20, got %d", cfg.WriteTimeoutSec)
	}
	if cfg.IdleTimeoutSec != 45 {
		t.Fatalf("expected idle timeout 45, got %d", cfg.IdleTimeoutSec)
	}
}
