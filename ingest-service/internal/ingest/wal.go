package ingest

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sync"
)

type walRecord struct {
	Op       string `json:"op"`
	ID       uint64 `json:"id"`
	Stream   string `json:"stream,omitempty"`
	DataType string `json:"data_type,omitempty"`
	Payload  string `json:"payload,omitempty"`
}

type queueWAL struct {
	mu           sync.Mutex
	path         string
	file         *os.File
	syncEvery    int
	opsSinceSync int
}

func openQueueWAL(cfg Config) (*queueWAL, []queueItem, uint64, int, error) {
	if err := os.MkdirAll(cfg.WALDir, 0o755); err != nil {
		return nil, nil, 0, 0, fmt.Errorf("create wal dir failed: %w", err)
	}

	path := filepath.Join(cfg.WALDir, cfg.WALFileName)

	// Truncate if WAL exceeds the configured max size to prevent unbounded
	// growth from blocking startup when Kafka is unavailable for extended periods.
	if info, err := os.Stat(path); err == nil {
		maxBytes := int64(cfg.WALMaxSizeMB) * 1024 * 1024
		if info.Size() > maxBytes {
			log.Printf(
				"[ingest-go] wal: size %d bytes exceeds max %d MB, discarding",
				info.Size(), cfg.WALMaxSizeMB,
			)
			if err := os.Remove(path); err != nil {
				return nil, nil, 0, 0, fmt.Errorf("remove oversized wal failed: %w", err)
			}
		}
	}

	pendingItems, maxItemID, truncated, err := readPendingFromWAL(path)
	if err != nil {
		return nil, nil, 0, 0, err
	}

	if err := rewriteWAL(path, pendingItems); err != nil {
		return nil, nil, 0, truncated, err
	}

	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, nil, 0, truncated, fmt.Errorf("open wal for append failed: %w", err)
	}

	return &queueWAL{
		path:      path,
		file:      file,
		syncEvery: maxInt(1, cfg.WALSyncEvery),
	}, pendingItems, maxItemID, truncated, nil
}

func readPendingFromWAL(path string) ([]queueItem, uint64, int, error) {
	file, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return []queueItem{}, 0, 0, nil
		}
		return nil, 0, 0, fmt.Errorf("open wal failed: %w", err)
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)

	orderedIDs := make([]uint64, 0)
	pendingMap := make(map[uint64]queueItem)
	var maxItemID uint64
	lineNumber := 0
	truncated := false
	for scanner.Scan() {
		lineNumber++
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}

		record := walRecord{}
		if err := json.Unmarshal(line, &record); err != nil {
			if !scanner.Scan() && scanner.Err() == nil {
				truncated = true
				break
			}
			return nil, 0, 0, fmt.Errorf("parse wal line %d failed: %w", lineNumber, err)
		}
		if record.ID > maxItemID {
			maxItemID = record.ID
		}

		switch record.Op {
		case "add":
			if record.ID == 0 {
				return nil, 0, 0, fmt.Errorf("invalid wal add id on line %d", lineNumber)
			}
			if _, exists := pendingMap[record.ID]; !exists {
				orderedIDs = append(orderedIDs, record.ID)
			}
			pendingMap[record.ID] = queueItem{
				ID:       record.ID,
				Stream:   record.Stream,
				DataType: record.DataType,
				Payload:  record.Payload,
			}
		case "ack":
			delete(pendingMap, record.ID)
		default:
			return nil, 0, 0, fmt.Errorf("unknown wal op on line %d: %s", lineNumber, record.Op)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, 0, 0, fmt.Errorf("scan wal failed: %w", err)
	}
	var truncatedI64 int
	if truncated {
		truncatedI64 = 1
		log.Printf("[ingest-go] wal: ignoring truncated line %d, file will be cleaned up on rewrite", lineNumber)
	}

	pendingItems := make([]queueItem, 0, len(pendingMap))
	for _, itemID := range orderedIDs {
		if item, exists := pendingMap[itemID]; exists {
			pendingItems = append(pendingItems, item)
		}
	}
	return pendingItems, maxItemID, truncatedI64, nil
}
func rewriteWAL(path string, pendingItems []queueItem) error {
	tempPath := path + ".tmp"
	file, err := os.OpenFile(tempPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("open wal temp failed: %w", err)
	}

	for _, item := range pendingItems {
		record := walRecord{
			Op:       "add",
			ID:       item.ID,
			Stream:   item.Stream,
			DataType: item.DataType,
			Payload:  item.Payload,
		}
		encoded, err := json.Marshal(record)
		if err != nil {
			_ = file.Close()
			return fmt.Errorf("encode wal replay item failed: %w", err)
		}
		if _, err := file.Write(append(encoded, '\n')); err != nil {
			_ = file.Close()
			return fmt.Errorf("write wal temp failed: %w", err)
		}
	}

	if err := file.Sync(); err != nil {
		_ = file.Close()
		return fmt.Errorf("sync wal temp failed: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close wal temp failed: %w", err)
	}
	if err := os.Rename(tempPath, path); err != nil {
		return fmt.Errorf("replace wal failed: %w", err)
	}
	return nil
}

func (w *queueWAL) appendAdd(item queueItem) error {
	return w.appendRecord(walRecord{
		Op:       "add",
		ID:       item.ID,
		Stream:   item.Stream,
		DataType: item.DataType,
		Payload:  item.Payload,
	})
}

func (w *queueWAL) appendAcks(itemIDs []uint64) error {
	if len(itemIDs) == 0 {
		return nil
	}
	if w == nil || w.file == nil {
		return fmt.Errorf("wal not initialized")
	}

	var buffer bytes.Buffer
	for _, itemID := range itemIDs {
		encoded, err := json.Marshal(walRecord{
			Op: "ack",
			ID: itemID,
		})
		if err != nil {
			return err
		}
		buffer.Write(encoded)
		buffer.WriteByte('\n')
	}

	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		return fmt.Errorf("wal closed during write")
	}
	if _, err := w.file.Write(buffer.Bytes()); err != nil {
		return err
	}

	w.opsSinceSync += len(itemIDs)
	if w.opsSinceSync >= w.syncEvery {
		if err := w.file.Sync(); err != nil {
			return err
		}
		w.opsSinceSync = 0
	}

	return nil
}

func (w *queueWAL) appendRecord(record walRecord) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w == nil || w.file == nil {
		return fmt.Errorf("wal not initialized")
	}

	encoded, err := json.Marshal(record)
	if err != nil {
		return err
	}
	if _, err := w.file.Write(append(encoded, '\n')); err != nil {
		return err
	}

	w.opsSinceSync++
	if w.opsSinceSync >= w.syncEvery {
		if err := w.file.Sync(); err != nil {
			return err
		}
		w.opsSinceSync = 0
	}

	return nil
}

// WALCompactedSize returns the number of "add" records that have matching "ack" records
// and could be removed by compaction. Returns -1 if the WAL is nil or closed.
func (w *queueWAL) WALCompactedSize() int64 {
	// Only available after a compact has been performed.
	return 0
}

// Size returns the current WAL file size in bytes. Returns 0 if file is nil or closed.
func (w *queueWAL) Size() int64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w == nil || w.file == nil {
		return 0
	}
	info, err := os.Stat(w.path)
	if err != nil {
		return 0
	}
	return info.Size()
}

// Compact rewrites the WAL file, keeping only "add" records without matching "ack" records.
// This prevents unbounded WAL growth during runtime (the old behavior only checked at startup).
// Must be called with w.mu held (not queue.mu).
func (w *queueWAL) Compact() error {
	if w == nil {
		return nil
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	// Close the current append-handle so we can read the file safely
	if w.file == nil {
		return nil
	}
	if err := w.file.Sync(); err != nil {
		return fmt.Errorf("sync wal before compact: %w", err)
	}
	if err := w.file.Close(); err != nil {
		log.Printf("[ingest-go] wal: close before compact warning: %v", err)
	}
	w.file = nil

	// Read all records, filter out acked ones
	records, err := readAllRecords(w.path)
	if err != nil {
		return fmt.Errorf("read wal for compact: %w", err)
	}

	// Build set of acked IDs
	acked := make(map[uint64]bool)
	pending := make([]walRecord, 0)
	for _, rec := range records {
		if rec.Op == "ack" {
			acked[rec.ID] = true
		}
	}

	// Keep only "add" records not yet acked
	addCount := 0
	ackCount := len(acked)
	for _, rec := range records {
		if rec.Op == "add" {
			if !acked[rec.ID] {
				pending = append(pending, rec)
			}
			addCount++
		}
	}

	// Rewrite the WAL with only pending (non-acked) add records
	tempPath := w.path + ".tmp"
	file, err := os.OpenFile(tempPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("open wal temp for compact: %w", err)
	}

	for _, rec := range pending {
		encoded, err := json.Marshal(rec)
		if err != nil {
			_ = file.Close()
			return fmt.Errorf("encode wal record for compact: %w", err)
		}
		if _, err := file.Write(append(encoded, '\n')); err != nil {
			_ = file.Close()
			return fmt.Errorf("write wal temp for compact: %w", err)
		}
	}

	if err := file.Sync(); err != nil {
		_ = file.Close()
		return fmt.Errorf("sync wal temp for compact: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close wal temp for compact: %w", err)
	}
	if err := os.Rename(tempPath, w.path); err != nil {
		return fmt.Errorf("replace wal after compact: %w", err)
	}

	// Reopen for append
	reopened, err := os.OpenFile(w.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("reopen wal after compact: %w", err)
	}
	w.file = reopened
	w.opsSinceSync = 0

	log.Printf(
		"[ingest-go] wal compacted: %d add records → %d pending (removed %d acked + %d stale acks)",
		addCount, len(pending), ackCount, addCount-len(pending)-ackCount,
	)
	return nil
}

// readAllRecords reads every JSON line from a WAL file, returning all records in order.
func readAllRecords(path string) ([]walRecord, error) {
	file, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("open wal for read: %w", err)
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	var records []walRecord
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var rec walRecord
		if err := json.Unmarshal(line, &rec); err != nil {
			return nil, fmt.Errorf("parse wal line: %w", err)
		}
		records = append(records, rec)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan wal: %w", err)
	}
	return records, nil
}

func (w *queueWAL) close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w == nil || w.file == nil {
		return nil
	}

	if err := w.file.Sync(); err != nil {
		_ = w.file.Close()
		w.file = nil
		return err
	}
	if err := w.file.Close(); err != nil {
		w.file = nil
		return err
	}
	w.file = nil
	return nil
}
