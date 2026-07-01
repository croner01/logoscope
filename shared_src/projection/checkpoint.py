from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import datetime


@dataclass
class ProjectionCheckpoint:
    """Projection 的消费进度——基于 partition + offset。"""
    projection: str
    epoch: str
    records: Dict[str, Dict[int, int]] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def update(self, topic: str, partition: int, offset: int):
        if topic not in self.records:
            self.records[topic] = {}
        current = self.records[topic].get(partition, -1)
        if offset > current:
            self.records[topic][partition] = offset
            self.updated_at = datetime.utcnow()

    def get_lag(self, topic: str, partition: int,
                latest_offset: int) -> int:
        current = self.records.get(topic, {}).get(partition, 0)
        return latest_offset - current

    def total_lag(self, topic_latest: Dict[str, Dict[int, int]]) -> int:
        total = 0
        for topic, partitions in topic_latest.items():
            for partition, latest in partitions.items():
                total += self.get_lag(topic, partition, latest)
        return total
