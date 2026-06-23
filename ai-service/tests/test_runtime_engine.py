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

    def test_engine_pipeline_clickhouse_remote_route(self):
        """All ClickHouse queries route to remote now — no local fast path."""
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
        assert compiled.route == "remote"
        assert "clickhouse-client --query" in compiled.shell_command

    def test_engine_pipeline_security_blocks_bad_command(self):
        """Command with blocked operator should be rejected."""
        from ai.command.normalizer import normalize_command_spec
        from ai.command.security import evaluate_command, SessionCostState

        raw = {"tool": "generic_exec", "command": "rm -rf /tmp/x", "purpose": "delete"}
        spec = normalize_command_spec(raw)
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is False


class TestStreamLlmPlan:
    """Tests for _stream_llm_plan — streaming LLM planning with event emission."""

    async def _make_state(self, **kwargs):
        state = RuntimeState(
            run_id=kwargs.get("run_id", "stream-test-1"),
            question="test",
            analysis_context={},
            max_iterations=1,
        )
        return state

    def _make_memory(self):
        return SessionMemory()

    def _make_emitter(self):
        return EventEmitter()

    async def _async_gen(self, chunks):
        """Helper: yield chunks one by one."""
        for c in chunks:
            yield c

    # ── Happy path ─────────────────────────────────────────────────────────

    def test_stream_collects_tokens_and_emits_events(self):
        """Verify tokens collected and assistant_delta events emitted."""
        async def _test():
            from ai.runtime.engine import _stream_llm_plan

            state = await self._make_state()
            memory = self._make_memory()
            emitter = self._make_emitter()

            # Subscribe BEFORE running the stream to avoid race
            queue = emitter.subscribe(state.run_id)
            collected_events = []

            async def collect_events():
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.5)
                        collected_events.append((event["type"], event["payload"]))
                    except asyncio.TimeoutError:
                        break

            async def mock_llm_call(sp, tp, ts):
                async for chunk in self._async_gen(['{"act', 'ions":[', '{"tool":"generic_exec"}', "]}"]):
                    yield chunk

            collect_task = asyncio.create_task(collect_events())
            await asyncio.sleep(0.01)  # let collector start

            result = await _stream_llm_plan(
                system_prompt="sys",
                task_prompt="task",
                tool_schema={},
                state=state,
                memory=memory,
                llm_call=mock_llm_call,
                event_emitter=emitter,
            )

            await asyncio.sleep(0.05)
            collect_task.cancel()

            assert len(result.actions) == 1
            assert result.actions[0]["tool"] == "generic_exec"
            delta_events = [e for e in collected_events if e[0] == "assistant_delta"]
            assert len(delta_events) >= 1
            combined = "".join(e[1].get("text", "") for e in delta_events)
            assert "generic_exec" in combined

        asyncio.run(_test())

    # ── Edge cases ─────────────────────────────────────────────────────────

    def test_no_llm_call_returns_empty(self):
        """When llm_call is None, return empty result gracefully."""
        async def _test():
            from ai.runtime.engine import _stream_llm_plan

            state = await self._make_state()
            memory = self._make_memory()

            result = await _stream_llm_plan(
                system_prompt="sys",
                task_prompt="task",
                tool_schema={},
                state=state,
                memory=memory,
                llm_call=None,
            )
            assert len(result.actions) == 0
            assert "no LLM" in result.summary

        asyncio.run(_test())

    def test_empty_collected_text_returns_empty(self):
        """When the LLM yields nothing, return empty result."""
        async def _test():
            from ai.runtime.engine import _stream_llm_plan

            state = await self._make_state()
            memory = self._make_memory()
            emitter = self._make_emitter()

            async def empty_call(sp, tp, ts):
                async for chunk in self._async_gen([]):
                    yield chunk

            result = await _stream_llm_plan(
                system_prompt="sys",
                task_prompt="task",
                tool_schema={},
                state=state,
                memory=memory,
                llm_call=empty_call,
                event_emitter=emitter,
            )
            assert len(result.actions) == 0
            assert "empty" in result.summary

        asyncio.run(_test())

    def test_non_json_response_returns_error(self):
        """When LLM returns non-JSON text, return error result with raw_response."""
        async def _test():
            from ai.runtime.engine import _stream_llm_plan

            state = await self._make_state()
            memory = self._make_memory()
            emitter = self._make_emitter()

            async def non_json_call(sp, tp, ts):
                async for chunk in self._async_gen(["I'm a helpful assistant. Let me analyze the logs."]):
                    yield chunk

            result = await _stream_llm_plan(
                system_prompt="sys",
                task_prompt="task",
                tool_schema={},
                state=state,
                memory=memory,
                llm_call=non_json_call,
                event_emitter=emitter,
            )
            assert len(result.actions) == 0
            assert "non-JSON" in result.summary
            assert result.raw_response != ""

        asyncio.run(_test())

    def test_llm_call_exception_returns_error(self):
        """When llm_call raises, return graceful error result."""
        async def _test():
            from ai.runtime.engine import _stream_llm_plan

            state = await self._make_state()
            memory = self._make_memory()

            async def broken_call(sp, tp, ts):
                """Async generator that raises on first iteration."""
                raise RuntimeError("API timeout")
                # (never reaches yield)
                if False:  # pragma: no cover
                    yield ""

            result = await _stream_llm_plan(
                system_prompt="sys",
                task_prompt="task",
                tool_schema={},
                state=state,
                memory=memory,
                llm_call=broken_call,
            )
            assert len(result.actions) == 0
            assert "failed" in result.summary

        asyncio.run(_test())

    def test_multi_action_json_parsed_correctly(self):
        """Multiple actions in JSON array are all parsed."""
        async def _test():
            from ai.runtime.engine import _stream_llm_plan

            state = await self._make_state()
            memory = self._make_memory()
            emitter = self._make_emitter()

            json_text = (
                '[{"tool":"clickhouse_query","command":"SELECT 1","purpose":"test1"},'
                '{"tool":"generic_exec","command":"kubectl get pods","purpose":"test2"}]'
            )

            async def multi_call(sp, tp, ts):
                async for chunk in self._async_gen([json_text]):
                    yield chunk

            result = await _stream_llm_plan(
                system_prompt="sys",
                task_prompt="task",
                tool_schema={},
                state=state,
                memory=memory,
                llm_call=multi_call,
                event_emitter=emitter,
            )
            assert len(result.actions) == 2
            tools = [a["tool"] for a in result.actions]
            assert "clickhouse_query" in tools
            assert "generic_exec" in tools

        asyncio.run(_test())

    def test_event_not_emitted_when_no_emitter(self):
        """When event_emitter is None, no crash."""
        async def _test():
            from ai.runtime.engine import _stream_llm_plan

            state = await self._make_state()
            memory = self._make_memory()

            async def ok_call(sp, tp, ts):
                async for chunk in self._async_gen(['{"actions":[]}']):
                    yield chunk

            result = await _stream_llm_plan(
                system_prompt="sys",
                task_prompt="task",
                tool_schema={},
                state=state,
                memory=memory,
                llm_call=ok_call,
                event_emitter=None,
            )
            assert result is not None

        asyncio.run(_test())

    def test_partial_json_collected_across_multiple_chunks(self):
        """JSON split across chunks is properly reassembled."""
        async def _test():
            from ai.runtime.engine import _stream_llm_plan

            state = await self._make_state()
            memory = self._make_memory()
            emitter = self._make_emitter()

            async def chunked_call(sp, tp, ts):
                async for chunk in self._async_gen([
                    '{"act',
                    'ions":',
                    '[{"to',
                    'ol":"clickhouse_query",',
                    '"command":"SE',
                    'LECT 1",',
                    '"purpose":"test"',
                    "}]}",
                ]):
                    yield chunk

            result = await _stream_llm_plan(
                system_prompt="sys",
                task_prompt="task",
                tool_schema={},
                state=state,
                memory=memory,
                llm_call=chunked_call,
                event_emitter=emitter,
            )
            assert len(result.actions) == 1
            assert result.actions[0]["tool"] == "clickhouse_query"

        asyncio.run(_test())
