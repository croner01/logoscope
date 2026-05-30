package ingest

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestWritePayloadsToMemoryRejectsBatchAtomically(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize:    3,
		MemoryQueueDropOldest: false,
	})

	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"old-1", "old-2"}); err != nil {
		t.Fatalf("preload memory queue failed: %v", err)
	}

	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"new-1", "new-2"}); err == nil {
		t.Fatalf("expected backpressure error")
	}

	if len(writer.memoryQueue) != 2 {
		t.Fatalf("expected queue size 2 after atomic reject, got %d", len(writer.memoryQueue))
	}
	if writer.memoryQueue[0].Payload != "old-1" || writer.memoryQueue[1].Payload != "old-2" {
		t.Fatalf("unexpected queue payloads after reject: %+v", writer.memoryQueue)
	}
	if writer.stats.MemoryQueued != 2 {
		t.Fatalf("expected memory_queued 2, got %d", writer.stats.MemoryQueued)
	}
	if writer.stats.BackpressureRejected != 1 {
		t.Fatalf("expected backpressure_rejected 1, got %d", writer.stats.BackpressureRejected)
	}
}

func TestWritePayloadsToMemoryDropOldestWhenFull(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize:    3,
		MemoryQueueDropOldest: true,
	})

	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"old-1", "old-2"}); err != nil {
		t.Fatalf("preload memory queue failed: %v", err)
	}

	results, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"new-1", "new-2", "new-3"})
	if err != nil {
		t.Fatalf("write payloads failed: %v", err)
	}

	if len(results) != 3 {
		t.Fatalf("expected 3 results, got %d", len(results))
	}
	if len(writer.memoryQueue) != 3 {
		t.Fatalf("expected queue size 3, got %d", len(writer.memoryQueue))
	}
	if writer.memoryQueue[0].Payload != "new-1" || writer.memoryQueue[1].Payload != "new-2" || writer.memoryQueue[2].Payload != "new-3" {
		t.Fatalf("unexpected queue payload order: %+v", writer.memoryQueue)
	}
	if writer.stats.Dropped != 2 {
		t.Fatalf("expected dropped 2, got %d", writer.stats.Dropped)
	}
	if writer.stats.MemoryQueued != 5 {
		t.Fatalf("expected memory_queued 5, got %d", writer.stats.MemoryQueued)
	}
}

func TestWritePayloadsToMemoryDropOldestWithZeroMaxDoesNotPanic(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize:    0,
		MemoryQueueDropOldest: true,
	})

	defer func() {
		if recovered := recover(); recovered != nil {
			t.Fatalf("expected no panic when max size is zero, got %v", recovered)
		}
	}()

	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"x"}); err == nil {
		t.Fatalf("expected backpressure error when max size is zero")
	}
	if writer.stats.BackpressureRejected != 1 {
		t.Fatalf("expected backpressure_rejected 1, got %d", writer.stats.BackpressureRejected)
	}
}

func TestPopHeadMemoryItemIfMatchLockedAvoidsWrongRemoval(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize:    4,
		MemoryQueueDropOldest: true,
	})

	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"old-1", "old-2"}); err != nil {
		t.Fatalf("preload memory queue failed: %v", err)
	}

	writer.mu.Lock()
	flushingID := writer.memoryQueue[0].ID
	_, _ = writer.dropOldestMemoryItemLocked()
	removed := writer.popHeadMemoryItemIfMatchLocked(flushingID)
	remaining := append([]queueItem(nil), writer.memoryQueue...)
	writer.mu.Unlock()

	if removed {
		t.Fatalf("expected stale flush ack not to remove new head")
	}
	if len(remaining) != 1 || remaining[0].Payload != "old-2" {
		t.Fatalf("unexpected queue after stale flush ack: %+v", remaining)
	}
}

func TestMemoryQueueItemIDIncrementsMonotonic(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize:    4,
		MemoryQueueDropOldest: false,
	})

	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"a", "b", "c"}); err != nil {
		t.Fatalf("write payloads failed: %v", err)
	}

	writer.mu.Lock()
	if len(writer.memoryQueue) != 3 {
		writer.mu.Unlock()
		t.Fatalf("expected 3 queue items, got %d", len(writer.memoryQueue))
	}
	first := writer.memoryQueue[0].ID
	second := writer.memoryQueue[1].ID
	third := writer.memoryQueue[2].ID
	writer.mu.Unlock()

	if !(first > 0 && second == first+1 && third == second+1) {
		t.Fatalf("unexpected queue item ids: %d, %d, %d", first, second, third)
	}
}

func TestPickMemoryQueueFlushBatchLockedKeepsHomogeneousStream(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize:        8,
		MemoryQueueFlushBatchSize: 4,
		MemoryQueueDropOldest:     false,
	})

	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"log-1", "log-2"}); err != nil {
		t.Fatalf("write logs payloads failed: %v", err)
	}
	if _, err := writer.writePayloadsToMemory("metrics.raw", "metrics", []string{"metric-1"}); err != nil {
		t.Fatalf("write metrics payload failed: %v", err)
	}

	writer.mu.Lock()
	batch := writer.pickMemoryQueueFlushBatchLocked()
	writer.mu.Unlock()

	if len(batch) != 2 {
		t.Fatalf("expected 2 homogeneous items in flush batch, got %d", len(batch))
	}
	if batch[0].Stream != "logs.raw" || batch[1].Stream != "logs.raw" {
		t.Fatalf("unexpected mixed stream in batch: %+v", batch)
	}
}

func TestPopHeadMemoryItemsIfMatchLockedStopsOnMismatch(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize:        8,
		MemoryQueueFlushBatchSize: 4,
		MemoryQueueDropOldest:     false,
	})

	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"a", "b", "c"}); err != nil {
		t.Fatalf("write payloads failed: %v", err)
	}

	writer.mu.Lock()
	if len(writer.memoryQueue) != 3 {
		writer.mu.Unlock()
		t.Fatalf("expected 3 items, got %d", len(writer.memoryQueue))
	}
	firstID := writer.memoryQueue[0].ID
	thirdID := writer.memoryQueue[2].ID
	popped := writer.popHeadMemoryItemsIfMatchLocked([]uint64{firstID, thirdID})
	remaining := append([]queueItem(nil), writer.memoryQueue...)
	writer.mu.Unlock()

	if len(popped) != 1 || popped[0] != firstID {
		t.Fatalf("expected only first item popped on mismatch, got %+v", popped)
	}
	if len(remaining) != 2 || remaining[0].Payload != "b" || remaining[1].Payload != "c" {
		t.Fatalf("unexpected queue state after mismatch pop: %+v", remaining)
	}
}

func TestWriteToQueueKafkaAsyncEnqueuesPayload(t *testing.T) {
	writer := NewQueueWriter(Config{
		QueueBackend:              "kafka",
		KafkaWriteMode:            "async",
		MemoryQueueMaxSize:        8,
		MemoryQueueFlushBatchSize: 2,
		QueueFlushIntervalMs:      100,
		QueueLogRecordBatchSize:   200,
	})
	defer writer.stopReconnectLoop()

	result, err := writer.WriteToQueue(
		context.Background(),
		"logs.raw",
		"logs",
		`{"log":"hello world","kubernetes":{"pod_name":"test-pod"}}`,
		map[string]any{},
	)
	if err != nil {
		t.Fatalf("write to queue failed: %v", err)
	}

	if result["mode"] != "memory_queue" {
		t.Fatalf("expected memory_queue mode, got %v", result["mode"])
	}

	writer.mu.Lock()
	queueSize := len(writer.memoryQueue)
	memoryQueued := writer.stats.MemoryQueued
	writer.mu.Unlock()

	if queueSize != 1 {
		t.Fatalf("expected queue size 1, got %d", queueSize)
	}
	if memoryQueued != 1 {
		t.Fatalf("expected memory_queued 1, got %d", memoryQueued)
	}
}

func TestWritePayloadsToMemoryRollbackDropWhenWALAddFails(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize:    1,
		MemoryQueueDropOldest: true,
	})

	writer.mu.Lock()
	writer.memoryQueue = append(writer.memoryQueue, queueItem{
		ID:       1,
		Stream:   "logs.raw",
		DataType: "logs",
		Payload:  "old-payload",
	})
	writer.nextMemoryItemID = 1
	writer.wal = &queueWAL{} // appendAdd/appendAcks 都会返回 wal not initialized
	writer.mu.Unlock()

	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"new-payload"}); err == nil {
		t.Fatalf("expected wal write failure when dropping oldest item")
	}

	writer.mu.Lock()
	defer writer.mu.Unlock()
	if len(writer.memoryQueue) != 1 {
		t.Fatalf("expected queue size to stay 1, got %d", len(writer.memoryQueue))
	}
	if writer.memoryQueue[0].Payload != "old-payload" {
		t.Fatalf("expected old payload to be rolled back, got %s", writer.memoryQueue[0].Payload)
	}
	if writer.stats.Dropped != 0 {
		t.Fatalf("expected dropped counter rolled back to 0, got %d", writer.stats.Dropped)
	}
}

func TestGetStatsUsesKafkaPingForQueuePingAge(t *testing.T) {
	writer := NewQueueWriter(Config{
		QueueBackend:         "kafka",
		MemoryQueueMaxSize:   10,
		KafkaWriteMode:       "async",
		QueueFlushIntervalMs: 100,
	})

	writer.mu.Lock()
	writer.kafkaConnected = true
	writer.kafkaLastPing = time.Now().Add(-2 * time.Second)
	writer.mu.Unlock()

	stats := writer.GetStats()

	if stats["queue_last_ping_age_seconds"] == nil {
		t.Fatalf("expected queue_last_ping_age_seconds for kafka backend")
	}
	if stats["queue_backend"] != "kafka" {
		t.Fatalf("expected queue backend kafka, got %v", stats["queue_backend"])
	}
}

func TestFinalizeFlushedBatchRollbackWhenWALAckFails(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize: 8,
	})

	writer.mu.Lock()
	writer.memoryQueue = append(writer.memoryQueue,
		queueItem{ID: 1, Stream: "logs.raw", DataType: "logs", Payload: "a"},
		queueItem{ID: 2, Stream: "logs.raw", DataType: "logs", Payload: "b"},
	)
	writer.wal = &queueWAL{} // appendAcks 会返回 wal not initialized
	batch := append([]queueItem(nil), writer.memoryQueue...)
	err := writer.finalizeFlushedBatchLocked(batch, []uint64{1, 2})
	queueSnapshot := append([]queueItem(nil), writer.memoryQueue...)
	stats := writer.stats
	writer.mu.Unlock()

	if err == nil {
		t.Fatalf("expected wal ack failure in finalizeFlushedBatchLocked")
	}
	if len(queueSnapshot) != 2 || queueSnapshot[0].ID != 1 || queueSnapshot[1].ID != 2 {
		t.Fatalf("expected queue rollback to original head items, got %+v", queueSnapshot)
	}
	if stats.MemoryQueueFlushed != 0 {
		t.Fatalf("expected memory_queue_flushed 0, got %d", stats.MemoryQueueFlushed)
	}
	if stats.MemoryQueueRequeued != 2 {
		t.Fatalf("expected memory_queue_requeued 2, got %d", stats.MemoryQueueRequeued)
	}
	if stats.MemoryQueueFlushFailures != 1 {
		t.Fatalf("expected memory_queue_flush_failures 1, got %d", stats.MemoryQueueFlushFailures)
	}
	if stats.WALWriteFailures == 0 {
		t.Fatalf("expected wal_write_failures incremented on ack failure")
	}
}

func TestFinalizeFlushedBatchSuccessWithoutWAL(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize: 8,
	})

	writer.mu.Lock()
	writer.memoryQueue = append(writer.memoryQueue,
		queueItem{ID: 1, Stream: "logs.raw", DataType: "logs", Payload: "a"},
		queueItem{ID: 2, Stream: "logs.raw", DataType: "logs", Payload: "b"},
	)
	batch := append([]queueItem(nil), writer.memoryQueue...)
	err := writer.finalizeFlushedBatchLocked(batch, []uint64{1, 2})
	queueSize := len(writer.memoryQueue)
	stats := writer.stats
	writer.mu.Unlock()

	if err != nil {
		t.Fatalf("expected finalizeFlushedBatchLocked success, got %v", err)
	}
	if queueSize != 0 {
		t.Fatalf("expected queue emptied after finalize success, got %d", queueSize)
	}
	if stats.MemoryQueueFlushed != 2 {
		t.Fatalf("expected memory_queue_flushed 2, got %d", stats.MemoryQueueFlushed)
	}
}

func TestRenderPrometheusMetricsKafkaOnly(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize: 8,
	})

	metrics := writer.RenderPrometheusMetrics()
	if !containsAll(metrics,
		"ingest_queue_kafka_written_total",
		"ingest_queue_kafka_connected",
		"ingest_queue_backend_info{backend=\"kafka\"} 1",
	) {
		t.Fatalf("expected kafka metrics in output, got metrics:\n%s", metrics)
	}
	if containsAny(metrics,
		"ingest_queue_stream_maxlen",
		"ingest_queue_"+"redis"+"_written_total",
		"ingest_queue_"+"redis"+"_connected",
		"ingest_queue_"+"redis"+"_write_blocked",
	) {
		t.Fatalf("expected redis metrics removed, got metrics:\n%s", metrics)
	}
}

func containsAll(content string, needles ...string) bool {
	for _, needle := range needles {
		if !strings.Contains(content, needle) {
			return false
		}
	}
	return true
}

func containsAny(content string, needles ...string) bool {
	for _, needle := range needles {
		if strings.Contains(content, needle) {
			return true
		}
	}
	return false
}

func TestMarkReconnectLoopStoppedLockedIgnoresStaleLoopID(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize: 8,
	})

	writer.mu.Lock()
	writer.reconnectRunning = true
	writer.reconnectLoopID = 2
	writer.reconnectCancel = func() {}
	writer.markReconnectLoopStoppedLocked(1)
	running := writer.reconnectRunning
	cancel := writer.reconnectCancel
	writer.mu.Unlock()

	if !running {
		t.Fatalf("expected reconnect loop to keep running on stale loop id")
	}
	if cancel == nil {
		t.Fatalf("expected reconnect cancel func kept on stale loop id")
	}
}

func TestMarkReconnectLoopStoppedLockedClearsActiveLoop(t *testing.T) {
	writer := NewQueueWriter(Config{
		MemoryQueueMaxSize: 8,
	})

	writer.mu.Lock()
	writer.reconnectRunning = true
	writer.reconnectLoopID = 3
	writer.reconnectCancel = func() {}
	writer.markReconnectLoopStoppedLocked(3)
	running := writer.reconnectRunning
	cancel := writer.reconnectCancel
	writer.mu.Unlock()

	if running {
		t.Fatalf("expected reconnect loop stopped for active loop id")
	}
	if cancel != nil {
		t.Fatalf("expected reconnect cancel func cleared for active loop id")
	}
}

func TestWritePayloadsToMemoryDropOldestWithWALEnabledReplayConsistent(t *testing.T) {
	tempDir := t.TempDir()
	cfg := Config{
		MemoryQueueMaxSize:    2,
		MemoryQueueDropOldest: true,
		WALEnabled:            true,
		WALDir:                tempDir,
		WALFileName:           "queue.wal",
		WALSyncEvery:          1,
	}

	writer := NewQueueWriter(cfg)
	if err := writer.Init(); err != nil {
		t.Fatalf("init writer failed: %v", err)
	}
	if _, err := writer.writePayloadsToMemory("logs.raw", "logs", []string{"a", "b", "c"}); err != nil {
		t.Fatalf("write payloads with drop oldest failed: %v", err)
	}
	writer.Shutdown(context.Background())

	restarted := NewQueueWriter(cfg)
	if err := restarted.Init(); err != nil {
		t.Fatalf("init restarted writer failed: %v", err)
	}
	defer restarted.Shutdown(context.Background())

	restarted.mu.Lock()
	got := []string{}
	for _, item := range restarted.memoryQueue {
		got = append(got, item.Payload)
	}
	restarted.mu.Unlock()

	if len(got) != 2 || got[0] != "b" || got[1] != "c" {
		t.Fatalf("expected replayed queue [b c], got %+v, wal=%s", got, filepath.Join(tempDir, "queue.wal"))
	}
}
