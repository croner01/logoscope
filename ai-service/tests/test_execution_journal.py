"""Tests for ExecutionJournal."""
from __future__ import annotations

from ai.agent_runtime.execution_journal import ExecutionJournal


class TestExecutionJournal:
    def test_fingerprint_is_deterministic(self):
        journal = ExecutionJournal()
        spec_a = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-abc -n islap --tail=100", "target_kind": "k8s_cluster", "target_identity": "pod:pod-abc/namespace:islap"},
        }
        spec_b = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-abc -n islap --tail=100", "target_kind": "k8s_cluster", "target_identity": "pod:pod-abc/namespace:islap"},
        }
        assert journal.fingerprint(spec_a) == journal.fingerprint(spec_b)

    def test_different_pods_produce_different_fingerprints(self):
        journal = ExecutionJournal()
        spec_a = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-abc", "target_kind": "k8s_cluster", "target_identity": "pod:pod-abc"},
        }
        spec_b = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-xyz", "target_kind": "k8s_cluster", "target_identity": "pod:pod-xyz"},
        }
        assert journal.fingerprint(spec_a) != journal.fingerprint(spec_b)

    def test_lookup_returns_none_for_unknown_fingerprint(self):
        journal = ExecutionJournal()
        assert journal.lookup("nonexistent") is None

    def test_record_and_lookup_roundtrip(self):
        journal = ExecutionJournal()
        fp = "abc123"
        journal.record(
            fingerprint=fp,
            command="kubectl get pods",
            target_kind="k8s_cluster",
            target_identity="namespace:islap",
            exit_code=0,
            summary="found 3 pods running",
            output_preview="NAME READY STATUS\npod-1 1/1 Running",
        )
        entry = journal.lookup(fp)
        assert entry is not None
        assert entry["fingerprint"] == "abc123"
        assert entry["exit_code"] == 0
        assert entry["summary"] == "found 3 pods running"
        assert "output_truncated_preview" in entry

    def test_duplicate_record_overwrites(self):
        journal = ExecutionJournal()
        fp = "dup123"
        journal.record(fp, "cmd1", "k8s_cluster", "ns:default", 1, "failed", "error output")
        journal.record(fp, "cmd2", "k8s_cluster", "ns:default", 0, "retry ok", "good output")
        entry = journal.lookup(fp)
        assert entry["exit_code"] == 0
        assert entry["summary"] == "retry ok"

    def test_context_for_llm_formats_correctly(self):
        journal = ExecutionJournal()
        journal.record("fp1", "kubectl logs pod-a", "k8s_cluster", "pod:pod-a", 0, "3 errors found", "...")
        journal.record("fp2", "kubectl describe pod pod-a", "k8s_cluster", "pod:pod-a", 0, "OOMKilled", "...")
        context = journal.context_for_llm()
        assert "kubectl logs pod-a" in context
        assert "3 errors found" in context
        assert "OOMKilled" in context
        assert "fp1" not in context  # fingerprints are internal

    def test_empty_journal_context_is_empty_string(self):
        journal = ExecutionJournal()
        assert journal.context_for_llm() == ""

    def test_to_list_and_from_summary_roundtrip(self):
        journal = ExecutionJournal()
        journal.record("fp1", "kubectl get pods", "k8s_cluster", "ns:default", 0, "ok", "...")
        journal.record("fp2", "kubectl logs pod-1", "k8s_cluster", "pod:pod-1", 1, "failed", "err...")

        summary = {"execution_journal": journal.to_list()}
        restored = ExecutionJournal.from_summary(summary)

        assert restored.lookup("fp1") is not None
        assert restored.lookup("fp2") is not None
        assert restored.lookup("fp1")["summary"] == "ok"
        assert restored.lookup("fp2")["exit_code"] == 1
