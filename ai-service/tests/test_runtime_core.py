"""Tests for runtime/state.py, runtime/memory.py, runtime/events.py."""
import asyncio
from ai.runtime.state import RuntimeState, Action, Observation, EvidenceSlot
from ai.runtime.memory import SessionMemory
from ai.runtime.events import EventEmitter
from ai.command.spec import CommandSpec, ToolType


class TestRuntimeState:
    def test_initial_state(self):
        state = RuntimeState(
            run_id="run-001",
            question="why is api-gateway returning 500?",
            analysis_context={"service_name": "api-gateway"},
            source_target={"pod_name": "api-gateway-abc", "namespace": "islap"},
        )
        assert state.iteration == 0
        assert state.phase == "planning"
        assert state.max_iterations == 4
        assert state.actions == []
        assert state.check_evidence_sufficient() is False

    def test_add_observation(self):
        state = RuntimeState(run_id="r1", question="test", analysis_context={})
        action = Action(
            action_id="a1",
            command_spec=CommandSpec(tool=ToolType.GENERIC_EXEC, command="kubectl get pods"),
            purpose="list pods",
        )
        obs = Observation(action_id="a1", status="completed", exit_code=0, stdout="pod-1\npod-2")
        state.actions.append(action)
        state.add_observation(action, obs)
        assert len(state.observations) == 1
        assert state.observations[0].exit_code == 0

    def test_evidence_sufficient_with_filled_slots(self):
        state = RuntimeState(run_id="r1", question="test", analysis_context={})
        state.evidence_slots["pods_status"] = EvidenceSlot(key="pods_status", status="filled")
        state.evidence_slots["logs"] = EvidenceSlot(key="logs", status="filled")
        assert state.check_evidence_sufficient() is True

    def test_evidence_insufficient_with_unfilled(self):
        state = RuntimeState(run_id="r1", question="test", analysis_context={})
        state.evidence_slots["pods_status"] = EvidenceSlot(key="pods_status", status="filled")
        state.evidence_slots["logs"] = EvidenceSlot(key="logs", status="pending")
        assert state.check_evidence_sufficient() is False

    def test_evidence_sufficient_without_slots(self):
        state = RuntimeState(run_id="r1", question="test", analysis_context={})
        obs1 = Observation(action_id="a1", exit_code=0)
        obs2 = Observation(action_id="a2", exit_code=0)
        state.observations = [obs1, obs2]
        assert state.check_evidence_sufficient() is True


class TestSessionMemory:
    def test_fingerprint_is_deterministic(self):
        mem = SessionMemory()
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl logs pod-a -n islap",
            target_identity="pod:pod-a/namespace:islap",
        )
        fp1 = mem.fingerprint(spec)
        fp2 = mem.fingerprint(spec)
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_is_duplicate_detects_repeat(self):
        mem = SessionMemory()
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl get pods",
            target_identity="namespace:islap",
        )
        mem.record(spec, exit_code=0, summary="ok", output_preview="...")
        assert mem.is_duplicate(spec) is True

    def test_is_duplicate_false_for_new(self):
        mem = SessionMemory()
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl get pods",
            target_identity="namespace:islap",
        )
        assert mem.is_duplicate(spec) is False

    def test_different_pods_different_fingerprint(self):
        mem = SessionMemory()
        a = CommandSpec(tool=ToolType.GENERIC_EXEC, command="kubectl logs pod-a", target_identity="pod:pod-a")
        b = CommandSpec(tool=ToolType.GENERIC_EXEC, command="kubectl logs pod-b", target_identity="pod:pod-b")
        assert mem.fingerprint(a) != mem.fingerprint(b)

    def test_context_for_llm(self):
        mem = SessionMemory()
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl logs pod-a",
            target_identity="pod:pod-a",
        )
        mem.record(spec, exit_code=0, summary="3 errors found", output_preview="ERROR: ...")
        ctx = mem.context_for_llm()
        assert "kubectl logs pod-a" in ctx
        assert "3 errors found" in ctx

    def test_record_blocked_is_duplicate(self):
        """Blocked commands are now duplicates to prevent LLM replan loops."""
        mem = SessionMemory()
        spec = CommandSpec(tool=ToolType.GENERIC_EXEC, command="rm -rf /")
        mem.record_blocked(spec, "head not in allowlist")
        assert mem.is_duplicate(spec) is True
        assert mem.was_previously_blocked(spec) is True


class TestEventEmitter:
    def test_emitter_creation(self):
        emitter = EventEmitter()
        assert len(emitter._queues) == 0

    def test_subscribe_and_emit(self):
        async def _test():
            emitter = EventEmitter()
            queue = emitter.subscribe("run-1")
            await emitter.emit("run-1", "action_result", {"status": "ok"})
            event = await asyncio.wait_for(queue.get(), timeout=1)
            assert event["type"] == "action_result"
            assert event["payload"]["status"] == "ok"
        asyncio.run(_test())

    def test_unsubscribe_removes_queue(self):
        emitter = EventEmitter()
        queue = emitter.subscribe("run-1")
        emitter.unsubscribe("run-1", queue)
        assert "run-1" not in emitter._queues
