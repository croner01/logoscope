package ingest

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestQueueWriterWALReplayAfterRestart(t *testing.T) {
	tempDir := t.TempDir()
	cfg := Config{
		MemoryQueueMaxSize:        8,
		MemoryQueueFlushBatchSize: 4,
		MemoryQueueDropOldest:     false,
		WALEnabled:                true,
		WALDir:                    tempDir,
		WALFileName:               "queue.wal",
		WALSyncEvery:              1,
	}

	first := NewQueueWriter(cfg)
	if err := first.Init(); err != nil {
		t.Fatalf("init first writer failed: %v", err)
	}
	if _, err := first.writePayloadsToMemory("logs.raw", "logs", []string{"log-1", "log-2"}); err != nil {
		t.Fatalf("write to memory failed: %v", err)
	}

	first.mu.Lock()
	if len(first.memoryQueue) != 2 {
		first.mu.Unlock()
		t.Fatalf("expected queue size 2, got %d", len(first.memoryQueue))
	}
	firstID := first.memoryQueue[0].ID
	poppedIDs := first.popHeadMemoryItemsIfMatchLocked([]uint64{firstID})
	if len(poppedIDs) != 1 {
		first.mu.Unlock()
		t.Fatalf("expected one popped id, got %+v", poppedIDs)
	}
	if err := first.appendWALAcksLocked(poppedIDs); err != nil {
		first.mu.Unlock()
		t.Fatalf("append wal ack failed: %v", err)
	}
	first.mu.Unlock()
	first.Shutdown(context.Background())

	second := NewQueueWriter(cfg)
	if err := second.Init(); err != nil {
		t.Fatalf("init second writer failed: %v", err)
	}
	defer second.Shutdown(context.Background())

	second.mu.Lock()
	replayedSize := len(second.memoryQueue)
	var payload string
	if replayedSize > 0 {
		payload = second.memoryQueue[0].Payload
	}
	recovered := second.stats.WALReplayRecovered
	second.mu.Unlock()

	if replayedSize != 1 {
		t.Fatalf("expected replayed queue size 1, got %d", replayedSize)
	}
	if payload != "log-2" {
		t.Fatalf("expected replayed payload log-2, got %s", payload)
	}
	if recovered != 1 {
		t.Fatalf("expected wal_replay_recovered 1, got %d", recovered)
	}
}

func TestQueueWriterInitFailsWhenWALDirIsFile(t *testing.T) {
	tempDir := t.TempDir()
	filePath := filepath.Join(tempDir, "not-a-dir")
	if err := os.WriteFile(filePath, []byte("x"), 0o644); err != nil {
		t.Fatalf("create blocker file failed: %v", err)
	}

	cfg := Config{
		MemoryQueueMaxSize:        8,
		MemoryQueueFlushBatchSize: 4,
		WALEnabled:                true,
		WALDir:                    filePath,
		WALFileName:               "queue.wal",
		WALSyncEvery:              1,
	}

	writer := NewQueueWriter(cfg)
	if err := writer.Init(); err == nil {
		t.Fatalf("expected wal init failure when wal dir is a file")
	}
}
