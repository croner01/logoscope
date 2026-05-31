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

func TestQueueWriterWALRecoversFromTruncatedLastLine(t *testing.T) {
	tempDir := t.TempDir()
	walPath := filepath.Join(tempDir, "queue.wal")

	validLine := `{"op":"add","id":1,"stream":"logs.raw","data_type":"logs","payload":"{\"message\":\"valid\"}"}` + "\n"
	truncatedLine := `{"op":"add","id":2,"stream":"logs.raw","data_type":"logs","payload":"{"` // truncated JSON

	if err := os.WriteFile(walPath, []byte(validLine+truncatedLine), 0o644); err != nil {
		t.Fatalf("write wal file failed: %v", err)
	}

	cfg := Config{
		MemoryQueueMaxSize:        8,
		MemoryQueueFlushBatchSize: 4,
		MemoryQueueDropOldest:     false,
		WALEnabled:                true,
		WALDir:                    tempDir,
		WALFileName:               "queue.wal",
		WALSyncEvery:              1,
	}

	writer := NewQueueWriter(cfg)
	if err := writer.Init(); err != nil {
		t.Fatalf("init should succeed despite truncated wal: %v", err)
	}
	defer writer.Shutdown(context.Background())

	writer.mu.Lock()
	replayedSize := len(writer.memoryQueue)
	payload := ""
	if replayedSize > 0 {
		payload = writer.memoryQueue[0].Payload
	}
	recovered := writer.stats.WALReplayRecovered
	writer.mu.Unlock()

	if replayedSize != 1 {
		t.Fatalf("expected 1 replayed item (truncated line 2 skipped), got %d", replayedSize)
	}
	if payload != `{"message":"valid"}` {
		t.Fatalf("expected valid payload, got %s", payload)
	}
	if recovered != 1 {
		t.Fatalf("expected wal_replay_recovered 1, got %d", recovered)
	}

	// Verify the WAL file was cleaned up by rewriteWAL
	cleaned, err := os.ReadFile(walPath)
	if err != nil {
		t.Fatalf("read cleaned wal failed: %v", err)
	}
	cleanedStr := string(cleaned)
	if len(cleanedStr) > 0 && cleanedStr[len(cleanedStr)-1] != '\n' {
		t.Fatalf("cleaned wal should end with newline, got trailing bytes")
	}
	// Count newlines: should be exactly 1 (the valid line)
	newlineCount := 0
	for _, b := range cleaned {
		if b == '\n' {
			newlineCount++
		}
	}
	if newlineCount != 1 {
		t.Fatalf("expected 1 line in cleaned wal, got %d newlines", newlineCount)
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
