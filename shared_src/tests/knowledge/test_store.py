import pytest
from shared_src.knowledge.models import SOP, FailurePattern
from shared_src.knowledge.store import KnowledgeMemoryStore
from shared_src.knowledge.memory import MemoryRecord


class TestKnowledgeStore:
    def test_add_and_retrieve(self):
        store = KnowledgeMemoryStore()
        store.add_document(SOP(
            document_id="sop-001",
            title="Nova OOM Troubleshooting",
            steps=["Check memory usage", "Migrate VMs"],
        ))
        results = store.retrieve("nova OOM")
        assert len(results) > 0
        assert results[0].document_id == "sop-001"

    def test_memory_write_and_retrieve(self):
        store = KnowledgeMemoryStore()
        store.add_memory(MemoryRecord(
            record_id="m1", record_type="repair", outcome="success",
            action_taken="restart neutron-dhcp-agent",
        ))
        results = store.retrieve("neutron agent failure")
        assert results[0].action_taken == "restart neutron-dhcp-agent"

    def test_retrieve_empty(self):
        store = KnowledgeMemoryStore()
        results = store.retrieve("nonexistent")
        assert results == []

    def test_multiple_documents(self):
        store = KnowledgeMemoryStore()
        store.add_document(SOP(document_id="s1", title="Restart", steps=[]))
        store.add_document(FailurePattern(document_id="fp1", title="OOM",
                            symptoms=[], root_cause="memory"))
        results = store.retrieve("restart")
        assert len(results) >= 1
