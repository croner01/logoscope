import pytest
from datetime import datetime
from shared_src.policy.decision_record import DecisionRecord, DecisionRecordStore


class TestDecisionRecord:
    def test_decision_record_fields(self):
        record = DecisionRecord(
            decision_id="dec-001", finding_id="f-001",
            context_hash="ctx_abc123",
        )
        assert record.finding_id == "f-001"
        assert record.context_hash == "ctx_abc123"

    def test_store_save_and_get(self):
        store = DecisionRecordStore()
        record = DecisionRecord(decision_id="dec-001", finding_id="f-001")
        store.save(record)
        retrieved = store.get("dec-001")
        assert retrieved is not None
        assert retrieved.decision_id == "dec-001"

    def test_get_by_finding(self):
        store = DecisionRecordStore()
        store.save(DecisionRecord(decision_id="d1", finding_id="f-001"))
        store.save(DecisionRecord(decision_id="d2", finding_id="f-001"))
        records = store.get_by_finding("f-001")
        assert len(records) == 2
