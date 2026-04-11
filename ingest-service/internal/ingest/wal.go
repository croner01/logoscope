package ingest

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type walRecord struct {
	Op       string `json:"op"`
	ID       uint64 `json:"id"`
	Stream   string `json:"stream,omitempty"`
	DataType string `json:"data_type,omitempty"`
	Payload  string `json:"payload,omitempty"`
}

type queueWAL struct {
	path         string
	file         *os.File
	syncEvery    int
	opsSinceSync int
}

func openQueueWAL(cfg Config) (*queueWAL, []queueItem, uint64, error) {
	if err := os.MkdirAll(cfg.WALDir, 0o755); err != nil {
		return nil, nil, 0, fmt.Errorf("create wal dir failed: %w", err)
	}

	path := filepath.Join(cfg.WALDir, cfg.WALFileName)
	pendingItems, maxItemID, err := readPendingFromWAL(path)
	if err != nil {
		return nil, nil, 0, err
	}

	if err := rewriteWAL(path, pendingItems); err != nil {
		return nil, nil, 0, err
	}

	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, nil, 0, fmt.Errorf("open wal for append failed: %w", err)
	}

	return &queueWAL{
		path:      path,
		file:      file,
		syncEvery: maxInt(1, cfg.WALSyncEvery),
	}, pendingItems, maxItemID, nil
}

func readPendingFromWAL(path string) ([]queueItem, uint64, error) {
	file, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return []queueItem{}, 0, nil
		}
		return nil, 0, fmt.Errorf("open wal failed: %w", err)
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)

	orderedIDs := make([]uint64, 0)
	pendingMap := make(map[uint64]queueItem)
	var maxItemID uint64
	lineNumber := 0
	for scanner.Scan() {
		lineNumber++
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}

		record := walRecord{}
		if err := json.Unmarshal(line, &record); err != nil {
			return nil, 0, fmt.Errorf("parse wal line %d failed: %w", lineNumber, err)
		}
		if record.ID > maxItemID {
			maxItemID = record.ID
		}

		switch record.Op {
		case "add":
			if record.ID == 0 {
				return nil, 0, fmt.Errorf("invalid wal add id on line %d", lineNumber)
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
			return nil, 0, fmt.Errorf("unknown wal op on line %d: %s", lineNumber, record.Op)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, 0, fmt.Errorf("scan wal failed: %w", err)
	}

	pendingItems := make([]queueItem, 0, len(pendingMap))
	for _, itemID := range orderedIDs {
		if item, exists := pendingMap[itemID]; exists {
			pendingItems = append(pendingItems, item)
		}
	}
	return pendingItems, maxItemID, nil
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

func (w *queueWAL) close() error {
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
