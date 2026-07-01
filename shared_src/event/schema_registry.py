from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any, Type
import json
from .envelope import EventEnvelope


@dataclass
class Schema:
    event_type: str
    version: int
    fields: Dict[str, Type]
    created_at: Optional[str] = None


@dataclass
class SchemaMigration:
    from_version: int
    to_version: int
    migrate_fn: Callable[[Dict], Dict]


class SchemaMigrationError(Exception):
    pass


class SchemaRegistry:
    def __init__(self):
        self._schemas: Dict[str, Dict[int, Schema]] = {}
        self._migrations: Dict[str, Dict[int, SchemaMigration]] = {}

    def register(self, event_type: str, version: int, schema: Schema):
        if event_type not in self._schemas:
            self._schemas[event_type] = {}
        self._schemas[event_type][version] = schema

    def register_migration(self, event_type: str,
                           from_version: int, to_version: int,
                           migrate_fn: Callable[[Dict], Dict]):
        key = event_type
        if key not in self._migrations:
            self._migrations[key] = {}
        self._migrations[key][from_version] = SchemaMigration(
            from_version=from_version,
            to_version=to_version,
            migrate_fn=migrate_fn,
        )

    def latest_version(self, event_type: str) -> int:
        versions = self._schemas.get(event_type, {})
        if not versions:
            return 1
        return max(versions.keys())

    def deserialize(self, envelope: EventEnvelope) -> Dict:
        payload = json.loads(envelope.payload.decode("utf-8"))
        current_version = envelope.schema_version
        latest = self.latest_version(envelope.event_type)

        while current_version < latest:
            migrations = self._migrations.get(envelope.event_type, {})
            migration = migrations.get(current_version)
            if not migration:
                raise SchemaMigrationError(
                    f"No migration from v{current_version} for {envelope.event_type}"
                )
            payload = migration.migrate_fn(payload)
            current_version = migration.to_version

        return payload

    def validate(self, event_type: str, version: int,
                 payload: Dict) -> bool:
        schema = self._schemas.get(event_type, {}).get(version)
        if not schema:
            return False
        for field_name, field_type in schema.fields.items():
            if field_name not in payload:
                return False
            if not isinstance(payload[field_name], field_type):
                return False
        return True

    def serialize(self, event_type: str, payload: Dict,
                  producer: str, **metadata) -> EventEnvelope:
        import uuid
        from datetime import datetime
        version = self.latest_version(event_type)
        return EventEnvelope(
            schema_version=version,
            event_type=event_type,
            producer=producer,
            event_id=uuid.uuid4().hex,
            timestamp=datetime.utcnow(),
            payload=json.dumps(payload).encode("utf-8"),
            metadata=metadata,
        )
