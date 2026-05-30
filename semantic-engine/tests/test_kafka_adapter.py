"""
KafkaQueue 分区批处理与提交语义测试
"""

import asyncio
from dataclasses import dataclass

from msgqueue.interface import Message
from msgqueue.kafka_adapter import KafkaQueue


@dataclass(frozen=True)
class _TopicPartition:
    topic: str
    partition: int


@dataclass
class _Record:
    topic: str
    partition: int
    offset: int
    value: bytes = b"{}"
    headers: list | None = None


class _DummyConsumer:
    async def commit(self, _payload):
        return None


def _build_queue() -> KafkaQueue:
    queue = object.__new__(KafkaQueue)
    queue._dlq_enabled = True
    queue._dlq_max_retries = 3
    queue.callback_offload = True
    queue.flush_offload = True
    queue.commit_error_as_warning = True
    queue.flush_retry_attempts = 1
    queue.flush_retry_delay_seconds = 0
    queue.max_retry_attempts = 3
    queue.retry_delay_seconds = 0
    return queue


def test_consume_partition_flush_once_and_commit_last_offset():
    queue = _build_queue()
    topic_partition = _TopicPartition(topic="logs.raw", partition=0)
    records = [_Record("logs.raw", 0, 10), _Record("logs.raw", 0, 11), _Record("logs.raw", 0, 12)]

    process_calls = []
    flush_calls = []
    commit_offsets = []

    async def fake_process(_callback, _message, record):
        process_calls.append(record.offset)
        return True, 1

    async def fake_flush(_callback):
        flush_calls.append("flush")
        return True

    async def fake_commit(_consumer, _tp, offset):
        commit_offsets.append(offset)

    queue._process_with_retry = fake_process
    queue._flush_pending_writes_with_retry = fake_flush
    queue._commit_record = fake_commit

    callback = lambda _message: True
    asyncio.run(queue._consume_partition_records(_DummyConsumer(), topic_partition, records, callback))

    assert process_calls == [10, 11, 12]
    assert flush_calls == ["flush"]
    assert commit_offsets == [12]


def test_consume_partition_stops_on_uncommittable_message():
    queue = _build_queue()
    queue._dlq_enabled = False

    topic_partition = _TopicPartition(topic="logs.raw", partition=1)
    records = [
        _Record("logs.raw", 1, 20),
        _Record("logs.raw", 1, 21),
        _Record("logs.raw", 1, 22),
        _Record("logs.raw", 1, 23),
    ]

    process_calls = []
    flush_calls = []
    commit_offsets = []

    async def fake_process(_callback, _message, record):
        process_calls.append(record.offset)
        if record.offset <= 21:
            return True, 1
        return False, 3

    async def fake_flush(_callback):
        flush_calls.append("flush")
        return True

    async def fake_commit(_consumer, _tp, offset):
        commit_offsets.append(offset)

    queue._process_with_retry = fake_process
    queue._flush_pending_writes_with_retry = fake_flush
    queue._commit_record = fake_commit

    callback = lambda _message: True
    asyncio.run(queue._consume_partition_records(_DummyConsumer(), topic_partition, records, callback))

    assert process_calls == [20, 21, 22]
    assert flush_calls == ["flush"]
    assert commit_offsets == [21]


def test_consume_partition_skips_commit_when_flush_failed():
    queue = _build_queue()
    topic_partition = _TopicPartition(topic="logs.raw", partition=2)
    records = [_Record("logs.raw", 2, 30), _Record("logs.raw", 2, 31)]

    flush_calls = []
    commit_offsets = []

    async def fake_process(_callback, _message, _record):
        return True, 1

    async def fake_flush(_callback):
        flush_calls.append("flush")
        return False

    async def fake_commit(_consumer, _tp, offset):
        commit_offsets.append(offset)

    queue._process_with_retry = fake_process
    queue._flush_pending_writes_with_retry = fake_flush
    queue._commit_record = fake_commit

    callback = lambda _message: True
    asyncio.run(queue._consume_partition_records(_DummyConsumer(), topic_partition, records, callback))

    assert flush_calls == ["flush"]
    assert commit_offsets == []


def test_consume_partition_dlq_only_commits_without_flush():
    queue = _build_queue()
    topic_partition = _TopicPartition(topic="logs.raw", partition=3)
    records = [_Record("logs.raw", 3, 40), _Record("logs.raw", 3, 41)]

    flush_calls = []
    commit_offsets = []
    moved_offsets = []

    async def fake_process(_callback, _message, _record):
        return False, 3

    async def fake_move_to_dlq(record, _message, _reason):
        moved_offsets.append(record.offset)
        return True

    async def fake_flush(_callback):
        flush_calls.append("flush")
        return True

    async def fake_commit(_consumer, _tp, offset):
        commit_offsets.append(offset)

    queue._process_with_retry = fake_process
    queue._move_to_dlq = fake_move_to_dlq
    queue._flush_pending_writes_with_retry = fake_flush
    queue._commit_record = fake_commit

    callback = lambda _message: True
    asyncio.run(queue._consume_partition_records(_DummyConsumer(), topic_partition, records, callback))

    assert moved_offsets == [40, 41]
    assert flush_calls == []
    assert commit_offsets == [41]


def test_build_message_decodes_headers():
    queue = _build_queue()
    record = _Record(
        topic="logs.raw",
        partition=0,
        offset=1,
        value=b'{"ok":true}',
        headers=[("tenant", b"default"), ("attempt", 2)],
    )

    message = queue._build_message(record)

    assert isinstance(message, Message)
    assert message.subject == "logs.raw"
    assert message.headers == {"tenant": "default", "attempt": "2"}


def test_rebalance_commit_error_matcher():
    queue = _build_queue()
    assert queue._is_rebalance_commit_error(RuntimeError("CommitFailedError due to rebalance")) is True
    assert queue._is_rebalance_commit_error(RuntimeError("normal error")) is False
