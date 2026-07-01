import pytest
from shared_src.knowledge.models import (
    KnowledgeDocument, SOP, Runbook, FailurePattern, Incident, RCA
)


class TestKnowledgeModels:
    def test_sop_type(self):
        sop = SOP(document_id="sop-001", title="Restart RabbitMQ",
                   steps=["Check status", "Restart service", "Verify"])
        assert sop.document_type == "sop"
        assert len(sop.steps) == 3

    def test_runbook_type(self):
        runbook = Runbook(document_id="rb-001", title="RabbitMQ Recovery",
                           category="messaging", severity="P1")
        assert runbook.document_type == "runbook"
        assert runbook.severity == "P1"

    def test_failure_pattern(self):
        fp = FailurePattern(document_id="fp-001", title="RabbitMQ heartbeat lost",
                             symptoms=["heartbeat timeout", "AMQP disconnected"],
                             root_cause="network partition")
        assert fp.document_type == "failure_pattern"
        assert "heartbeat timeout" in fp.symptoms
        assert fp.root_cause == "network partition"

    def test_incident(self):
        inc = Incident(document_id="inc-001", title="Production outage",
                        severity="P0", duration_minutes=45)
        assert inc.document_type == "incident"
        assert inc.severity == "P0"

    def test_rca(self):
        rca = RCA(document_id="rca-001", title="Root cause: network partition",
                   finding="Network switch misconfiguration",
                   recommendation="Add redundant network paths")
        assert rca.document_type == "rca"
        assert "switch" in rca.finding

    def test_base_class(self):
        doc = KnowledgeDocument(document_id="generic-1", title="Generic doc")
        assert doc.document_id == "generic-1"
        assert doc.document_type == "knowledge"

    def test_trust_level(self):
        doc = KnowledgeDocument(document_id="kb-001", title="Official Guide",
                                 origin="openstack-official", trust_level=5)
        assert doc.trust_level == 5
