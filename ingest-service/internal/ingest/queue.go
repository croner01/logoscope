package ingest

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"

	"github.com/segmentio/kafka-go"
)

// QueueBackpressureError 表示降级内存队列已满。
type QueueBackpressureError struct {
	Message string
}

func (e *QueueBackpressureError) Error() string {
	if strings.TrimSpace(e.Message) == "" {
		return "ingest queue is full"
	}
	return e.Message
}

type queueItem struct {
	ID       uint64
	Stream   string
	DataType string
	Payload  string
}

type queueStats struct {
	TotalWritten             int64
	KafkaWritten             int64
	MemoryQueued             int64
	Dropped                  int64
	BackpressureRejected     int64
	ReconnectAttempts        int64
	MemoryQueueFlushed       int64
	MemoryQueueFlushFailures int64
	MemoryQueueRequeued      int64
	MemoryQueueHighWatermark int64
	WALAppended              int64
	WALAcked                 int64
	WALReplayRecovered       int64
	WALWriteFailures         int64
	LastError                string
}

// QueueWriter 负责写入 Kafka，并支持内存队列降级。
type QueueWriter struct {
	cfg Config

	mu sync.Mutex

	kafkaConnected   bool
	kafkaConnecting  bool
	kafkaLastPing    time.Time
	kafkaWriters     map[string]*kafka.Writer
	flushNotify      chan struct{}
	reconnectCancel  context.CancelFunc
	reconnectRunning bool
	reconnectLoopID  uint64
	nextMemoryItemID uint64
	wal              *queueWAL
	memoryQueue      []queueItem
	stats            queueStats
}

func NewQueueWriter(cfg Config) *QueueWriter {
	return &QueueWriter{
		cfg:          cfg,
		memoryQueue:  make([]queueItem, 0, cfg.MemoryQueueMaxSize),
		kafkaWriters: make(map[string]*kafka.Writer),
		flushNotify:  make(chan struct{}, 1),
	}
}

func (w *QueueWriter) Init() error {
	recoveredCount := 0
	if w.cfg.WALEnabled {
		wal, replayItems, maxItemID, err := openQueueWAL(w.cfg)
		if err != nil {
			return err
		}

		w.mu.Lock()
		w.wal = wal
		if maxItemID > w.nextMemoryItemID {
			w.nextMemoryItemID = maxItemID
		}
		if len(replayItems) > 0 {
			w.memoryQueue = append(w.memoryQueue, replayItems...)
			recoveredCount = len(replayItems)
			w.stats.WALReplayRecovered = int64(len(replayItems))
			if int64(len(w.memoryQueue)) > w.stats.MemoryQueueHighWatermark {
				w.stats.MemoryQueueHighWatermark = int64(len(w.memoryQueue))
			}
		}
		w.mu.Unlock()

		log.Printf("[ingest-go] wal enabled path=%s recovered=%d", wal.path, len(replayItems))
	}

	log.Printf(
		"[ingest-go] queue writer initialized (backend=kafka memory_queue_max_size=%d)",
		w.cfg.MemoryQueueMaxSize,
	)

	if w.cfg.KafkaAsyncEnabled() {
		w.startReconnectLoop()
		if recoveredCount > 0 {
			w.requestFlush()
		}
	}

	return nil
}

func (w *QueueWriter) Shutdown(ctx context.Context) {
	w.stopReconnectLoop()
	w.flushMemoryQueue(ctx)

	w.mu.Lock()
	kafkaWriters := make([]*kafka.Writer, 0, len(w.kafkaWriters))
	for _, writer := range w.kafkaWriters {
		kafkaWriters = append(kafkaWriters, writer)
	}
	wal := w.wal
	w.wal = nil
	w.kafkaConnected = false
	w.kafkaConnecting = false
	w.kafkaLastPing = time.Time{}
	w.kafkaWriters = make(map[string]*kafka.Writer)
	remaining := len(w.memoryQueue)
	w.mu.Unlock()

	if remaining > 0 {
		log.Printf("[ingest-go] shutdown with %d buffered items in memory queue", remaining)
	}

	for _, writer := range kafkaWriters {
		if writer == nil {
			continue
		}
		if err := writer.Close(); err != nil {
			log.Printf("[ingest-go] close kafka writer failed: %v", err)
		}
	}
	if wal != nil {
		if err := wal.close(); err != nil {
			log.Printf("[ingest-go] close wal failed: %v", err)
		}
	}
}

func (w *QueueWriter) WriteToQueue(ctx context.Context, stream string, dataType string, payload string, metadata map[string]any) (map[string]any, error) {
	w.mu.Lock()
	w.stats.TotalWritten++
	w.mu.Unlock()

	meta := map[string]any{}
	for key, value := range metadata {
		if key == "_parsed_payload" {
			continue
		}
		meta[key] = value
	}

	var payloadObj any
	if parsed, ok := metadata["_parsed_payload"]; ok {
		payloadObj = parsed
	}
	if payloadObj == nil {
		trimmedPayload := strings.TrimSpace(payload)
		shouldDecodeJSON := dataType == "logs" || strings.HasPrefix(trimmedPayload, "{") || strings.HasPrefix(trimmedPayload, "[")
		if shouldDecodeJSON {
			var decoded any
			if err := json.Unmarshal([]byte(payload), &decoded); err == nil {
				payloadObj = decoded
			}
		}
	}

	totalRecordCount := 1
	queueMessages := make([]map[string]any, 0, 1)
	if dataType == "logs" {
		logRecords := buildLogQueueMessages(payloadObj, payload, meta)
		totalRecordCount = len(logRecords)
		queueMessages = buildLogBatchPayloads(logRecords, w.cfg.QueueLogRecordBatchSize)
	} else {
		queueMessages = append(queueMessages, buildNonLogQueueMessage(dataType, payloadObj, payload))
	}

	queuePayloads := make([]string, 0, len(queueMessages))
	for _, message := range queueMessages {
		encoded, err := json.Marshal(message)
		if err != nil {
			return nil, err
		}
		queuePayloads = append(queuePayloads, string(encoded))
	}

	if w.cfg.KafkaAsyncEnabled() {
		results, err := w.writePayloadsToMemory(stream, dataType, queuePayloads)
		if err != nil {
			return nil, err
		}

		w.startReconnectLoop()
		w.requestFlush()

		if len(results) == 1 {
			return results[0], nil
		}
		return map[string]any{
			"status":        "success",
			"stream":        stream,
			"message_count": len(results),
			"record_count":  totalRecordCount,
			"results":       results,
			"mode":          "batch",
		}, nil
	}

	primaryAvailable := w.ensurePrimaryConnection(ctx)
	results := make([]map[string]any, 0, len(queuePayloads))

	if primaryAvailable {
		if len(queuePayloads) == 1 {
			result, err := w.writeToPrimary(ctx, stream, dataType, queuePayloads[0])
			if err == nil {
				results = append(results, result)
			} else {
				w.handlePrimaryWriteError(err)
				fallbackResults, fallbackErr := w.writePayloadsToMemory(stream, dataType, queuePayloads)
				if fallbackErr != nil {
					return nil, fallbackErr
				}
				results = append(results, fallbackResults...)
				w.startReconnectLoop()
			}
		} else {
			ids, err := w.writeBatchToPrimary(ctx, stream, dataType, queuePayloads)
			if err == nil {
				for _, messageID := range ids {
					results = append(results, map[string]any{
						"status":     "success",
						"stream":     stream,
						"message_id": messageID,
						"mode":       "kafka",
					})
				}
			} else {
				w.handlePrimaryWriteError(err)
				fallbackResults, fallbackErr := w.writePayloadsToMemory(stream, dataType, queuePayloads)
				if fallbackErr != nil {
					return nil, fallbackErr
				}
				results = append(results, fallbackResults...)
				w.startReconnectLoop()
			}
		}
	} else {
		fallbackResults, err := w.writePayloadsToMemory(stream, dataType, queuePayloads)
		if err != nil {
			return nil, err
		}
		results = append(results, fallbackResults...)
		w.startReconnectLoop()
	}

	if len(results) == 1 {
		return results[0], nil
	}

	return map[string]any{
		"status":        "success",
		"stream":        stream,
		"message_count": len(results),
		"record_count":  totalRecordCount,
		"results":       results,
		"mode":          "batch",
	}, nil
}

func (w *QueueWriter) writePayloadsToMemory(stream string, dataType string, payloads []string) ([]map[string]any, error) {
	if len(payloads) == 0 {
		return []map[string]any{}, nil
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	if !w.cfg.MemoryQueueDropOldest && len(w.memoryQueue)+len(payloads) > w.cfg.MemoryQueueMaxSize {
		w.stats.BackpressureRejected++
		return nil, &QueueBackpressureError{Message: "ingest queue is full, please retry"}
	}

	results := make([]map[string]any, 0, len(payloads))
	for _, payload := range payloads {
		if len(w.memoryQueue) >= w.cfg.MemoryQueueMaxSize {
			if w.cfg.MemoryQueueDropOldest {
				if len(w.memoryQueue) == 0 {
					w.stats.BackpressureRejected++
					return nil, &QueueBackpressureError{Message: "ingest queue is full, please retry"}
				}
				droppedItem := w.memoryQueue[0]
				item := w.newMemoryQueueItemLocked(stream, dataType, payload)

				if err := w.appendWALAddLocked(item); err != nil {
					return nil, err
				}
				if err := w.appendWALAcksLocked([]uint64{droppedItem.ID}); err != nil {
					// 尝试补偿 WAL add，避免“返回失败但重启后回放出新日志”。
					if rollbackErr := w.appendWALAcksLocked([]uint64{item.ID}); rollbackErr != nil {
						w.stats.LastError = fmt.Sprintf("%v; wal rollback ack failed: %v", err, rollbackErr)
					}
					return nil, err
				}

				w.memoryQueue[0] = queueItem{}
				w.memoryQueue = append(w.memoryQueue[1:], item)
				w.stats.Dropped++
				w.stats.MemoryQueued++
				if int64(len(w.memoryQueue)) > w.stats.MemoryQueueHighWatermark {
					w.stats.MemoryQueueHighWatermark = int64(len(w.memoryQueue))
				}

				results = append(results, map[string]any{
					"status":     "success",
					"stream":     stream,
					"mode":       "memory_queue",
					"queue_size": len(w.memoryQueue),
				})
				continue
			}

			w.stats.BackpressureRejected++
			return nil, &QueueBackpressureError{Message: "ingest queue is full, please retry"}
		}

		item := w.newMemoryQueueItemLocked(stream, dataType, payload)
		if err := w.appendWALAddLocked(item); err != nil {
			return nil, err
		}

		w.memoryQueue = append(w.memoryQueue, item)
		w.stats.MemoryQueued++
		if int64(len(w.memoryQueue)) > w.stats.MemoryQueueHighWatermark {
			w.stats.MemoryQueueHighWatermark = int64(len(w.memoryQueue))
		}

		results = append(results, map[string]any{
			"status":     "success",
			"stream":     stream,
			"mode":       "memory_queue",
			"queue_size": len(w.memoryQueue),
		})
	}

	return results, nil
}

func (w *QueueWriter) appendWALAddLocked(item queueItem) error {
	if w.wal == nil {
		return nil
	}
	if err := w.wal.appendAdd(item); err != nil {
		w.stats.WALWriteFailures++
		w.stats.LastError = err.Error()
		return err
	}
	w.stats.WALAppended++
	return nil
}

func (w *QueueWriter) appendWALAcksLocked(itemIDs []uint64) error {
	if w.wal == nil || len(itemIDs) == 0 {
		return nil
	}
	if err := w.wal.appendAcks(itemIDs); err != nil {
		w.stats.WALWriteFailures++
		w.stats.LastError = err.Error()
		return err
	}
	w.stats.WALAcked += int64(len(itemIDs))
	return nil
}

func (w *QueueWriter) newMemoryQueueItemLocked(stream string, dataType string, payload string) queueItem {
	w.nextMemoryItemID++
	return queueItem{
		ID:       w.nextMemoryItemID,
		Stream:   stream,
		DataType: dataType,
		Payload:  payload,
	}
}

func (w *QueueWriter) dropOldestMemoryItemLocked() (queueItem, bool) {
	if len(w.memoryQueue) == 0 {
		return queueItem{}, false
	}
	item := w.memoryQueue[0]
	w.memoryQueue[0] = queueItem{}
	w.memoryQueue = w.memoryQueue[1:]
	w.stats.Dropped++
	return item, true
}

func (w *QueueWriter) popHeadMemoryItemIfMatchLocked(itemID uint64) bool {
	if len(w.memoryQueue) == 0 || w.memoryQueue[0].ID != itemID {
		return false
	}
	w.memoryQueue[0] = queueItem{}
	w.memoryQueue = w.memoryQueue[1:]
	return true
}

func (w *QueueWriter) pickMemoryQueueFlushBatchLocked() []queueItem {
	if len(w.memoryQueue) == 0 {
		return nil
	}

	batchSize := w.cfg.MemoryQueueFlushBatchSize
	if batchSize <= 0 {
		batchSize = 1
	}
	if batchSize > len(w.memoryQueue) {
		batchSize = len(w.memoryQueue)
	}

	head := w.memoryQueue[0]
	selected := make([]queueItem, 0, batchSize)
	for _, item := range w.memoryQueue[:batchSize] {
		if item.Stream != head.Stream || item.DataType != head.DataType {
			break
		}
		selected = append(selected, item)
	}
	return selected
}

func (w *QueueWriter) popHeadMemoryItemsIfMatchLocked(itemIDs []uint64) []uint64 {
	if len(itemIDs) == 0 {
		return []uint64{}
	}
	popped := make([]uint64, 0, len(itemIDs))
	for _, itemID := range itemIDs {
		if len(w.memoryQueue) == 0 || w.memoryQueue[0].ID != itemID {
			break
		}
		w.memoryQueue[0] = queueItem{}
		w.memoryQueue = w.memoryQueue[1:]
		popped = append(popped, itemID)
	}
	return popped
}

func (w *QueueWriter) ensurePrimaryConnection(ctx context.Context) bool {
	return w.ensureKafkaConnection(ctx)
}

func (w *QueueWriter) handlePrimaryWriteError(err error) {
	w.mu.Lock()
	w.kafkaConnected = false
	w.stats.LastError = err.Error()
	w.mu.Unlock()
}

func (w *QueueWriter) writeToPrimary(ctx context.Context, stream string, dataType string, payload string) (map[string]any, error) {
	return w.writeToKafka(ctx, stream, dataType, payload)
}

func (w *QueueWriter) writeBatchToPrimary(ctx context.Context, stream string, dataType string, payloads []string) ([]string, error) {
	return w.writeBatchToKafka(ctx, stream, dataType, payloads)
}

func (w *QueueWriter) ensureKafkaConnection(ctx context.Context) bool {
	w.mu.Lock()
	connected := w.kafkaConnected
	lastPing := w.kafkaLastPing
	connecting := w.kafkaConnecting
	w.mu.Unlock()

	if connected {
		if time.Since(lastPing) < w.cfg.KafkaPingInterval() {
			return true
		}
		if err := w.connectKafka(ctx); err == nil {
			return true
		}
		w.mu.Lock()
		w.kafkaConnected = false
		w.stats.LastError = "kafka ping failed"
		w.mu.Unlock()
	}

	if connecting {
		return false
	}

	w.mu.Lock()
	w.kafkaConnecting = true
	w.mu.Unlock()
	defer func() {
		w.mu.Lock()
		w.kafkaConnecting = false
		w.mu.Unlock()
	}()

	attempts := w.cfg.KafkaMaxReconnectAttempts
	for attempt := 1; attempt <= attempts; attempt++ {
		w.mu.Lock()
		w.stats.ReconnectAttempts++
		w.mu.Unlock()

		if err := w.connectKafka(ctx); err == nil {
			return true
		}

		w.mu.Lock()
		w.stats.LastError = "kafka connect failed"
		w.mu.Unlock()
		time.Sleep(w.cfg.KafkaReconnectInterval())
	}
	return false
}

func (w *QueueWriter) connectKafka(ctx context.Context) error {
	if len(w.cfg.KafkaBrokers) == 0 {
		return errors.New("kafka brokers is empty")
	}

	dialer := &kafka.Dialer{}
	var lastError error
	for _, broker := range w.cfg.KafkaBrokers {
		dialCtx, cancel := context.WithTimeout(ctx, time.Duration(w.cfg.KafkaDialTimeout)*time.Second)
		conn, err := dialer.DialContext(dialCtx, "tcp", broker)
		cancel()
		if err != nil {
			lastError = err
			continue
		}
		_ = conn.Close()
		lastError = nil
		break
	}
	if lastError != nil {
		return fmt.Errorf("dial kafka brokers failed: %w", lastError)
	}

	w.mu.Lock()
	w.kafkaConnected = true
	w.kafkaLastPing = time.Now()
	w.stats.LastError = ""
	w.mu.Unlock()
	return nil
}

func (w *QueueWriter) kafkaWriterForTopicLocked(topic string) *kafka.Writer {
	if writer, ok := w.kafkaWriters[topic]; ok && writer != nil {
		return writer
	}

	writer := &kafka.Writer{
		Addr:         kafka.TCP(w.cfg.KafkaBrokers...),
		Topic:        topic,
		RequiredAcks: kafka.RequiredAcks(w.cfg.KafkaRequiredAcks),
		Async:        false,
		BatchSize:    w.cfg.KafkaBatchSize,
		BatchBytes:   int64(w.cfg.KafkaBatchBytes),
		BatchTimeout: time.Duration(w.cfg.KafkaBatchTimeoutMs) * time.Millisecond,
		Balancer:     &kafka.LeastBytes{},
	}
	w.kafkaWriters[topic] = writer
	return writer
}

func (w *QueueWriter) writeToKafka(ctx context.Context, stream string, dataType string, payload string) (map[string]any, error) {
	w.mu.Lock()
	writer := w.kafkaWriterForTopicLocked(stream)
	w.mu.Unlock()

	if writer == nil {
		return nil, errors.New("kafka writer not initialized")
	}

	writeCtx, cancel := context.WithTimeout(ctx, time.Duration(w.cfg.KafkaWriteTimeout)*time.Second)
	defer cancel()

	now := time.Now().UTC()
	messageID := fmt.Sprintf("kafka-%d", now.UnixNano())
	err := writer.WriteMessages(
		writeCtx,
		kafka.Message{
			Key:   []byte(messageID),
			Value: []byte(payload),
			Time:  now,
			Headers: []kafka.Header{
				{Key: "data_type", Value: []byte(dataType)},
				{Key: "ingest_time", Value: []byte(now.Format(time.RFC3339Nano))},
			},
		},
	)
	if err != nil {
		return nil, err
	}

	w.mu.Lock()
	w.kafkaConnected = true
	w.kafkaLastPing = time.Now()
	w.stats.KafkaWritten++
	w.stats.LastError = ""
	w.mu.Unlock()

	return map[string]any{
		"status":     "success",
		"stream":     stream,
		"message_id": messageID,
		"mode":       "kafka",
	}, nil
}

func (w *QueueWriter) writeBatchToKafka(ctx context.Context, stream string, dataType string, payloads []string) ([]string, error) {
	w.mu.Lock()
	writer := w.kafkaWriterForTopicLocked(stream)
	w.mu.Unlock()
	if writer == nil {
		return nil, errors.New("kafka writer not initialized")
	}

	writeCtx, cancel := context.WithTimeout(ctx, time.Duration(w.cfg.KafkaWriteTimeout)*time.Second)
	defer cancel()

	now := time.Now().UTC()
	messages := make([]kafka.Message, 0, len(payloads))
	messageIDs := make([]string, 0, len(payloads))
	for _, payload := range payloads {
		messageID := fmt.Sprintf("kafka-%d", now.UnixNano()+int64(len(messageIDs)))
		messageIDs = append(messageIDs, messageID)
		messages = append(messages, kafka.Message{
			Key:   []byte(messageID),
			Value: []byte(payload),
			Time:  now,
			Headers: []kafka.Header{
				{Key: "data_type", Value: []byte(dataType)},
				{Key: "ingest_time", Value: []byte(now.Format(time.RFC3339Nano))},
			},
		})
	}

	if err := writer.WriteMessages(writeCtx, messages...); err != nil {
		return nil, err
	}

	w.mu.Lock()
	w.kafkaConnected = true
	w.kafkaLastPing = time.Now()
	w.stats.KafkaWritten += int64(len(payloads))
	w.stats.LastError = ""
	w.mu.Unlock()
	return messageIDs, nil
}

func (w *QueueWriter) writeToMemoryQueue(stream string, dataType string, payload string) (map[string]any, error) {
	results, err := w.writePayloadsToMemory(stream, dataType, []string{payload})
	if err != nil {
		return nil, err
	}
	if len(results) == 0 {
		return nil, errors.New("memory queue write returned no result")
	}
	return results[0], nil
}

func (w *QueueWriter) flushMemoryQueue(ctx context.Context) {
	if !w.ensurePrimaryConnection(ctx) {
		return
	}

	for {
		w.mu.Lock()
		batch := w.pickMemoryQueueFlushBatchLocked()
		if len(batch) == 0 {
			w.mu.Unlock()
			return
		}
		w.mu.Unlock()

		payloads := make([]string, 0, len(batch))
		itemIDs := make([]uint64, 0, len(batch))
		for _, item := range batch {
			payloads = append(payloads, item.Payload)
			itemIDs = append(itemIDs, item.ID)
		}

		var flushErr error
		if len(payloads) == 1 {
			_, flushErr = w.writeToPrimary(ctx, batch[0].Stream, batch[0].DataType, payloads[0])
		} else {
			_, flushErr = w.writeBatchToPrimary(ctx, batch[0].Stream, batch[0].DataType, payloads)
		}
		if flushErr != nil {
			w.mu.Lock()
			w.kafkaConnected = false
			w.stats.LastError = flushErr.Error()
			w.stats.MemoryQueueFlushFailures++
			w.stats.MemoryQueueRequeued += int64(len(batch))
			w.mu.Unlock()
			return
		}

		w.mu.Lock()
		if err := w.finalizeFlushedBatchLocked(batch, itemIDs); err != nil {
			w.mu.Unlock()
			return
		}
		w.mu.Unlock()
	}
}

func (w *QueueWriter) finalizeFlushedBatchLocked(batch []queueItem, itemIDs []uint64) error {
	flushedIDs := w.popHeadMemoryItemsIfMatchLocked(itemIDs)
	if len(flushedIDs) == 0 {
		return nil
	}
	if err := w.appendWALAcksLocked(flushedIDs); err != nil {
		// WAL ack 失败时回滚弹出的队列项，避免成功写主队列后因 ack 丢失导致重启重复回放。
		rollbackCount := len(flushedIDs)
		if rollbackCount > len(batch) {
			rollbackCount = len(batch)
		}
		if rollbackCount > 0 {
			rollbackItems := append([]queueItem(nil), batch[:rollbackCount]...)
			w.memoryQueue = append(rollbackItems, w.memoryQueue...)
		}
		w.stats.MemoryQueueFlushFailures++
		w.stats.MemoryQueueRequeued += int64(rollbackCount)
		return err
	}
	w.stats.MemoryQueueFlushed += int64(len(flushedIDs))
	return nil
}

func (w *QueueWriter) requestFlush() {
	select {
	case w.flushNotify <- struct{}{}:
	default:
	}
}

func (w *QueueWriter) startReconnectLoop() {
	w.mu.Lock()
	if w.reconnectRunning {
		w.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	w.reconnectLoopID++
	loopID := w.reconnectLoopID
	w.reconnectCancel = cancel
	w.reconnectRunning = true
	w.mu.Unlock()

	go func(id uint64) {
		ticker := time.NewTicker(w.cfg.QueueFlushInterval())
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				w.mu.Lock()
				w.markReconnectLoopStoppedLocked(id)
				w.mu.Unlock()
				return
			case <-w.flushNotify:
			case <-ticker.C:
			}

			if w.ensurePrimaryConnection(context.Background()) {
				w.flushMemoryQueue(context.Background())
			}
		}
	}(loopID)
}

func (w *QueueWriter) markReconnectLoopStoppedLocked(loopID uint64) {
	if w.reconnectLoopID != loopID {
		return
	}
	w.reconnectRunning = false
	w.reconnectCancel = nil
}

func (w *QueueWriter) stopReconnectLoop() {
	w.mu.Lock()
	cancel := w.reconnectCancel
	w.reconnectCancel = nil
	w.reconnectRunning = false
	w.reconnectLoopID++
	w.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (w *QueueWriter) GetStats() map[string]any {
	w.mu.Lock()
	defer w.mu.Unlock()

	memorySize := len(w.memoryQueue)
	fillRatio := 0.0
	if w.cfg.MemoryQueueMaxSize > 0 {
		fillRatio = float64(memorySize) / float64(w.cfg.MemoryQueueMaxSize)
	}

	mode := "degraded"
	if w.kafkaConnected {
		mode = "normal"
	}

	queueLastPingAge := any(nil)
	if !w.kafkaLastPing.IsZero() {
		queueLastPingAge = roundFloat(time.Since(w.kafkaLastPing).Seconds(), 3)
	}

	return map[string]any{
		"total_written":               w.stats.TotalWritten,
		"queue_backend":               "kafka",
		"queue_written":               w.stats.KafkaWritten,
		"queue_connected":             w.kafkaConnected,
		"queue_connecting":            w.kafkaConnecting,
		"queue_last_ping_age_seconds": queueLastPingAge,
		"kafka_written":               w.stats.KafkaWritten,
		"memory_queued":               w.stats.MemoryQueued,
		"dropped":                     w.stats.Dropped,
		"backpressure_rejected":       w.stats.BackpressureRejected,
		"reconnect_attempts":          w.stats.ReconnectAttempts,
		"memory_queue_flushed":        w.stats.MemoryQueueFlushed,
		"memory_queue_flush_failures": w.stats.MemoryQueueFlushFailures,
		"memory_queue_requeued":       w.stats.MemoryQueueRequeued,
		"memory_queue_high_watermark": w.stats.MemoryQueueHighWatermark,
		"wal_enabled":                 w.wal != nil,
		"wal_appended":                w.stats.WALAppended,
		"wal_acked":                   w.stats.WALAcked,
		"wal_replay_recovered":        w.stats.WALReplayRecovered,
		"wal_write_failures":          w.stats.WALWriteFailures,
		"last_error":                  nullIfEmpty(w.stats.LastError),
		"kafka_connected":             w.kafkaConnected,
		"kafka_connecting":            w.kafkaConnecting,
		"memory_queue_size":           memorySize,
		"memory_queue_max_size":       w.cfg.MemoryQueueMaxSize,
		"memory_queue_fill_ratio":     roundFloat(fillRatio, 6),
		"reconnect_loop_running":      w.reconnectRunning,
		"mode":                        mode,
	}
}

func (w *QueueWriter) RenderPrometheusMetrics() string {
	stats := w.GetStats()

	mode := asString(stats["mode"])
	modeNormal := 0
	modeDegraded := 0
	switch mode {
	case "normal":
		modeNormal = 1
	default:
		modeDegraded = 1
	}

	lines := []string{
		"# HELP ingest_queue_total_written_total Total queue write attempts.",
		"# TYPE ingest_queue_total_written_total counter",
		fmt.Sprintf("ingest_queue_total_written_total %v", stats["total_written"]),
		"# HELP ingest_queue_primary_written_total Total successful writes to configured primary queue backend.",
		"# TYPE ingest_queue_primary_written_total counter",
		fmt.Sprintf("ingest_queue_primary_written_total %v", stats["queue_written"]),
		"# HELP ingest_queue_kafka_written_total Total successful Kafka writes.",
		"# TYPE ingest_queue_kafka_written_total counter",
		fmt.Sprintf("ingest_queue_kafka_written_total %v", stats["kafka_written"]),
		"# HELP ingest_queue_memory_queued_total Total items buffered into memory queue.",
		"# TYPE ingest_queue_memory_queued_total counter",
		fmt.Sprintf("ingest_queue_memory_queued_total %v", stats["memory_queued"]),
		"# HELP ingest_queue_dropped_total Total dropped queue items.",
		"# TYPE ingest_queue_dropped_total counter",
		fmt.Sprintf("ingest_queue_dropped_total %v", stats["dropped"]),
		"# HELP ingest_queue_backpressure_rejected_total Total writes rejected by backpressure.",
		"# TYPE ingest_queue_backpressure_rejected_total counter",
		fmt.Sprintf("ingest_queue_backpressure_rejected_total %v", stats["backpressure_rejected"]),
		"# HELP ingest_queue_wal_appended_total Total WAL add records appended.",
		"# TYPE ingest_queue_wal_appended_total counter",
		fmt.Sprintf("ingest_queue_wal_appended_total %v", stats["wal_appended"]),
		"# HELP ingest_queue_wal_acked_total Total WAL ack records appended.",
		"# TYPE ingest_queue_wal_acked_total counter",
		fmt.Sprintf("ingest_queue_wal_acked_total %v", stats["wal_acked"]),
		"# HELP ingest_queue_wal_write_failures_total Total WAL write failures.",
		"# TYPE ingest_queue_wal_write_failures_total counter",
		fmt.Sprintf("ingest_queue_wal_write_failures_total %v", stats["wal_write_failures"]),
		"# HELP ingest_queue_memory_queue_size Current memory queue size.",
		"# TYPE ingest_queue_memory_queue_size gauge",
		fmt.Sprintf("ingest_queue_memory_queue_size %v", stats["memory_queue_size"]),
		"# HELP ingest_queue_memory_queue_fill_ratio Current memory queue fill ratio.",
		"# TYPE ingest_queue_memory_queue_fill_ratio gauge",
		fmt.Sprintf("ingest_queue_memory_queue_fill_ratio %v", stats["memory_queue_fill_ratio"]),
		"# HELP ingest_queue_kafka_connected Kafka connection state.",
		"# TYPE ingest_queue_kafka_connected gauge",
		fmt.Sprintf("ingest_queue_kafka_connected %d", boolToInt(asBool(stats["kafka_connected"]))),
		"# HELP ingest_queue_connected Configured queue backend connection state.",
		"# TYPE ingest_queue_connected gauge",
		fmt.Sprintf("ingest_queue_connected %d", boolToInt(asBool(stats["queue_connected"]))),
		"# HELP ingest_queue_mode_info Queue mode gauge.",
		"# TYPE ingest_queue_mode_info gauge",
		fmt.Sprintf("ingest_queue_mode_info{mode=\"normal\"} %d", modeNormal),
		fmt.Sprintf("ingest_queue_mode_info{mode=\"degraded\"} %d", modeDegraded),
		fmt.Sprintf("ingest_queue_backend_info{backend=\"kafka\"} %d", boolToInt(asString(stats["queue_backend"]) == "kafka")),
	}

	return strings.Join(lines, "\n") + "\n"
}

func roundFloat(value float64, digits int) float64 {
	multiplier := mathPow10(digits)
	return float64(int64(value*multiplier+0.5)) / multiplier
}

func mathPow10(digits int) float64 {
	result := 1.0
	for i := 0; i < digits; i++ {
		result *= 10
	}
	return result
}

func nullIfEmpty(value string) any {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	return value
}

func boolToInt(value bool) int {
	if value {
		return 1
	}
	return 0
}
