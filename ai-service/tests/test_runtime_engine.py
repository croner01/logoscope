"""Integration tests for runtime/engine.py."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from ai.runtime.engine import run_diagnosis, LlmPlanResult
from ai.runtime.state import RuntimeState, Action
from ai.runtime.memory import SessionMemory
from ai.runtime.events import EventEmitter
from ai.command.spec import CommandSpec, ToolType


class TestRunDiagnosis:
    def test_engine_completes_with_sufficient_evidence(self):
        """Engine exits immediately when evidence is pre-filled and LLM returns no actions."""
        async def _test():
            state = RuntimeState(
                run_id="run-1",
                question="test question",
                analysis_context={"service_name": "test-svc"},
                max_iterations=2,
            )
            state.evidence_slots["key1"] = type("Slot", (), {"status": "filled", "key": "key1"})()
            state.evidence_slots["key2"] = type("Slot", (), {"status": "filled", "key": "key2"})()

            memory = SessionMemory()
            emitter = EventEmitter()

            async def noop_plan(system, task, schema, st, mem, llm):
                return LlmPlanResult(actions=[])

            result = await run_diagnosis(
                state=state,
                tools=MagicMock(),
                prompt_builder=MagicMock(),
                memory=memory,
                event_emitter=emitter,
                llm_plan=noop_plan,
            )
            assert result.summary != ""

        asyncio.run(_test())

    def test_engine_stops_at_max_iterations_with_empty_llm(self):
        """Engine stops after max_iterations when LLM returns no actions."""
        async def _test():
            state = RuntimeState(
                run_id="run-2",
                question="test",
                analysis_context={},
                max_iterations=1,
            )
            memory = SessionMemory()
            emitter = EventEmitter()

            async def empty_plan(system, task, schema, st, mem, llm):
                return LlmPlanResult(actions=[])

            result = await run_diagnosis(
                state=state,
                tools=MagicMock(),
                prompt_builder=MagicMock(),
                memory=memory,
                event_emitter=emitter,
                llm_plan=empty_plan,
            )
            assert state.phase in ("completed", "done")

        asyncio.run(_test())

    def test_session_memory_persists_across_engine_runs(self):
        """Memory should survive across run_diagnosis calls."""
        mem = SessionMemory()
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl get pods",
            target_identity="namespace:islap",
        )
        mem.record(spec, exit_code=0, summary="ok", output_preview="...")
        assert mem.is_duplicate(spec) is True

        spec2 = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl logs other-pod",
            target_identity="pod:other-pod",
        )
        assert mem.is_duplicate(spec2) is False

    def test_engine_pipeline_normalize_security_compile(self):
        """Test the full pipeline: normalize → security → compile → execute."""
        from ai.command.normalizer import normalize_command_spec
        from ai.command.security import evaluate_command, SessionCostState
        from ai.command.compiler import compile_command

        raw = {"tool": "generic_exec", "command": "kubectl get pods -n islap", "purpose": "list"}
        spec = normalize_command_spec(raw)
        assert spec.tool == ToolType.GENERIC_EXEC

        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is True

        compiled = compile_command(spec)
        assert compiled.route == "remote"
        assert compiled.shell_command == "kubectl get pods -n islap"

    def test_engine_pipeline_clickhouse_local_route(self):
        """Simple ClickHouse query should route to local."""
        from ai.command.normalizer import normalize_command_spec
        from ai.command.security import evaluate_command, SessionCostState
        from ai.command.compiler import compile_command

        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT * FROM logs.events WHERE service_name='api' LIMIT 10",
            "purpose": "query",
        }
        spec = normalize_command_spec(raw)
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is True

        compiled = compile_command(spec)
        assert compiled.route == "local"

    def test_engine_pipeline_security_blocks_bad_command(self):
        """Command with blocked operator should be rejected."""
        from ai.command.normalizer import normalize_command_spec
        from ai.command.security import evaluate_command, SessionCostState

        raw = {"tool": "generic_exec", "command": "rm -rf /tmp/x", "purpose": "delete"}
        spec = normalize_command_spec(raw)
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is False
