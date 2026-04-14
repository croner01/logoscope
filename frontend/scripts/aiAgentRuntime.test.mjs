import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeAgentRunEventEnvelope,
  parseAgentRuntimeEventBlock,
  takeNextSSEEventBlock,
} from '../.tmp-tests/utils/aiAgentRuntime.js';
import {
  buildRuntimeAnalysisContext,
  buildRuntimeDowngradeNotice,
} from '../.tmp-tests/utils/runtimeAnalysisMode.js';
import { buildRuntimeFollowUpContext } from '../.tmp-tests/utils/runtimeFollowUpContext.js';
import {
  buildHistoryTurns,
  filterRuntimeTurnsByThread,
  getConversationIdFromRun,
  isSameThreadIdentity,
  mergeHistoryTurnsWithRuntimeTurns,
  normalizeThreadIdentity,
  shouldHydrateHistoryForThreadSwitch,
} from '../.tmp-tests/utils/aiRuntimeThread.js';
import {
  createNextRuntimeStreamToken,
  shouldHandleRuntimeStreamMutation,
} from '../.tmp-tests/utils/aiRuntimeStream.js';
import { buildRuntimePresentation } from '../.tmp-tests/features/ai-runtime/utils/runtimePresentation.js';
import { buildRuntimeTranscriptMessage } from '../.tmp-tests/features/ai-runtime/utils/runtimeTranscript.js';
import { isTerminalAgentRunStatus } from '../.tmp-tests/features/ai-runtime/utils/runtimeView.js';
import {
  agentRunReducer,
  createInitialAgentRunState,
  selectAssistantMessage,
  selectCommandRuns,
  selectPendingApprovals,
} from '../.tmp-tests/utils/aiAgentRuntimeReducer.js';

test('parseAgentRuntimeEventBlock parses canonical SSE event block', () => {
  const parsed = parseAgentRuntimeEventBlock([
    'event: assistant_delta',
    'data: {"run_id":"run-001","seq":3,"event_type":"assistant_delta","payload":{"assistant_message_id":"msg-001","text":"hello"}}',
  ].join('\n'));

  assert.deepEqual(parsed, {
    event: 'assistant_delta',
    data: {
      run_id: 'run-001',
      seq: 3,
      event_type: 'assistant_delta',
      payload: {
        assistant_message_id: 'msg-001',
        text: 'hello',
      },
    },
  });
});

test('takeNextSSEEventBlock splits stream buffer incrementally', () => {
  const first = takeNextSSEEventBlock('event: x\ndata: {"a":1}\n\nevent: y\ndata: {"b":2}\n\n');
  assert.equal(first.block, 'event: x\ndata: {"a":1}');
  const second = takeNextSSEEventBlock(first.rest);
  assert.equal(second.block, 'event: y\ndata: {"b":2}');
});

test('buildRuntimeAnalysisContext clears dirty trace_id after downgrade', () => {
  const context = buildRuntimeAnalysisContext({
    analysisType: 'trace',
    traceId: '   ',
    serviceName: 'query-service',
    baseContext: { agent_mode: 'followup_analysis_runtime' },
  });

  assert.deepEqual(context, {
    agent_mode: 'followup_analysis_runtime',
    analysis_type: 'log',
    analysis_type_original: 'trace',
    analysis_type_downgraded: true,
    analysis_type_downgrade_reason: 'trace_id_missing',
    service_name: 'query-service',
  });
});

test('buildRuntimeDowngradeNotice explains trace downgrade clearly', () => {
  assert.equal(
    buildRuntimeDowngradeNotice('trace_id_missing'),
    '未检测到 Trace ID，已自动降级为日志分析（使用时间窗口）。',
  );
  assert.equal(buildRuntimeDowngradeNotice(undefined), '');
});

test('buildRuntimeFollowUpContext carries explicit evidence window and anchor aliases', () => {
  const context = buildRuntimeFollowUpContext({
    analysisSessionId: 'sess-001',
    analysisType: 'log',
    serviceName: 'query-service',
    inputText: 'ERROR request failed',
    question: '为什么失败',
    llmInfo: { method: 'llm' },
    result: { overview: { problem: 'clickhouse_query_error' } },
    detectedTraceId: '',
    detectedRequestId: '',
    sourceLogTimestamp: '2026-04-12T13:31:14Z',
    sourceTraceId: '',
    sourceRequestId: '',
    followupRelatedMeta: {
      followup_related_anchor_utc: '2026-04-12T13:31:14Z',
      followup_related_start_time: '2026-04-12T13:26:14Z',
      followup_related_end_time: '2026-04-12T13:36:14Z',
      followup_related_request_id: 'req-123',
    },
  });

  assert.equal(context.request_id, 'req-123');
  assert.equal(context.related_log_anchor_timestamp, '2026-04-12T13:31:14Z');
  assert.equal(context.request_flow_window_start, '2026-04-12T13:26:14Z');
  assert.equal(context.request_flow_window_end, '2026-04-12T13:36:14Z');
});

test('normalizeAgentRunEventEnvelope rejects invalid payloads', () => {
  assert.equal(normalizeAgentRunEventEnvelope(null), null);
  assert.equal(normalizeAgentRunEventEnvelope({ event_type: 'assistant_delta' }), null);
  assert.deepEqual(
    normalizeAgentRunEventEnvelope({
      run_id: 'run-001',
      seq: 4,
      event_type: 'assistant_delta',
      payload: { text: 'ok' },
    }),
    {
      run_id: 'run-001',
      seq: 4,
      event_type: 'assistant_delta',
      payload: { text: 'ok' },
      event_id: undefined,
      created_at: undefined,
    },
  );
});

test('thread helpers normalize identity and compare with exact session + conversation match', () => {
  const left = normalizeThreadIdentity({
    sessionId: ' sess-001 ',
    conversationId: ' conv-001 ',
  });
  const same = normalizeThreadIdentity({
    sessionId: 'sess-001',
    conversationId: 'conv-001',
  });
  const differentConversation = normalizeThreadIdentity({
    sessionId: 'sess-001',
    conversationId: '',
  });

  assert.deepEqual(left, { sessionId: 'sess-001', conversationId: 'conv-001' });
  assert.equal(isSameThreadIdentity(left, same), true);
  assert.equal(isSameThreadIdentity(left, differentConversation), false);
});

test('getConversationIdFromRun falls back to runtime_options conversation id', () => {
  assert.equal(getConversationIdFromRun({
    run_id: 'run-001',
    session_id: 'sess-001',
    conversation_id: 'conv-direct',
    analysis_type: 'log',
    engine: 'agent-runtime-v1',
    runtime_version: 'v1',
    user_message_id: 'msg-u-001',
    assistant_message_id: 'msg-a-001',
    status: 'running',
    question: '排查超时',
  }), 'conv-direct');

  assert.equal(getConversationIdFromRun({
    run_id: 'run-002',
    session_id: 'sess-002',
    analysis_type: 'log',
    engine: 'agent-runtime-v1',
    runtime_version: 'v1',
    user_message_id: 'msg-u-002',
    assistant_message_id: 'msg-a-002',
    status: 'running',
    question: '排查超时',
    summary_json: {
      runtime_options: {
        conversation_id: 'conv-fallback',
      },
    },
  }), 'conv-fallback');
});

test('buildHistoryTurns pairs user and assistant messages and uses assistant message id as turn id', () => {
  const turns = buildHistoryTurns({
    messages: [
      {
        message_id: 'msg-u-001',
        role: 'user',
        content: '第一次提问',
        timestamp: '2026-03-19T10:00:00Z',
      },
      {
        message_id: 'msg-a-001',
        role: 'assistant',
        content: '第一次回答',
        timestamp: '2026-03-19T10:00:05Z',
      },
      {
        message_id: 'msg-a-002',
        role: 'assistant',
        content: '补充回答',
        timestamp: '2026-03-19T10:00:06Z',
      },
    ],
  });

  assert.deepEqual(turns, [
    {
      turnId: 'msg-a-001',
      question: '第一次提问',
      answer: '第一次回答',
      userMessageId: 'msg-u-001',
      assistantMessageId: 'msg-a-001',
      timestamp: '2026-03-19T10:00:05Z',
    },
    {
      turnId: 'msg-a-002',
      question: '',
      answer: '补充回答',
      userMessageId: undefined,
      assistantMessageId: 'msg-a-002',
      timestamp: '2026-03-19T10:00:06Z',
    },
  ]);
});

test('buildHistoryTurns rejects mismatched conversation detail', () => {
  const turns = buildHistoryTurns({
    messages: [
      { role: 'user', content: '追问' },
      { role: 'assistant', content: '回答' },
    ],
    preferredConversationId: 'conv-001',
    detailConversationId: 'conv-002',
  });

  assert.deepEqual(turns, []);
});

test('filterRuntimeTurnsByThread keeps only exact session + conversation matches', () => {
  const runtimeTurns = filterRuntimeTurnsByThread([
    { turnId: 'turn-1', sessionId: 'sess-001', conversationId: 'conv-001' },
    { turnId: 'turn-2', sessionId: 'sess-001', conversationId: '' },
    { turnId: 'turn-3', sessionId: 'sess-002', conversationId: 'conv-001' },
  ], {
    sessionId: 'sess-001',
    conversationId: 'conv-001',
  });

  assert.deepEqual(runtimeTurns, [
    { turnId: 'turn-1', sessionId: 'sess-001', conversationId: 'conv-001' },
  ]);
});

test('mergeHistoryTurnsWithRuntimeTurns removes history turns already represented by runtime turns', () => {
  const merged = mergeHistoryTurnsWithRuntimeTurns(
    [
      { turnId: 'turn-history-1', answer: '旧回答' },
      { turnId: 'turn-runtime-1', answer: '将被 runtime 替代' },
    ],
    [
      { turnId: 'turn-runtime-1', source: 'runtime' },
      { turnId: 'turn-runtime-2', source: 'runtime' },
    ],
  );

  assert.deepEqual(merged, [
    { turnId: 'turn-history-1', answer: '旧回答' },
    { turnId: 'turn-runtime-1', source: 'runtime' },
    { turnId: 'turn-runtime-2', source: 'runtime' },
  ]);
});

test('shouldHydrateHistoryForThreadSwitch triggers on empty thread or identity switch only', () => {
  const currentIdentity = { sessionId: 'sess-001', conversationId: 'conv-001' };
  const sameIdentity = { sessionId: 'sess-001', conversationId: 'conv-001' };
  const differentIdentity = { sessionId: 'sess-001', conversationId: 'conv-002' };

  assert.equal(shouldHydrateHistoryForThreadSwitch({
    currentIdentity,
    nextIdentity: sameIdentity,
    currentTurnCount: 0,
    nextSessionId: 'sess-001',
  }), true);

  assert.equal(shouldHydrateHistoryForThreadSwitch({
    currentIdentity,
    nextIdentity: sameIdentity,
    currentTurnCount: 2,
    nextSessionId: 'sess-001',
  }), false);

  assert.equal(shouldHydrateHistoryForThreadSwitch({
    currentIdentity,
    nextIdentity: differentIdentity,
    currentTurnCount: 2,
    nextSessionId: 'sess-001',
  }), true);

  assert.equal(shouldHydrateHistoryForThreadSwitch({
    currentIdentity,
    nextIdentity: differentIdentity,
    currentTurnCount: 2,
    nextSessionId: '',
  }), false);
});

test('runtime stream token increments monotonically from any numeric input', () => {
  assert.equal(createNextRuntimeStreamToken(0), 1);
  assert.equal(createNextRuntimeStreamToken(3), 4);
  assert.equal(createNextRuntimeStreamToken(-2), 1);
});

test('shouldHandleRuntimeStreamMutation rejects stale token or inactive run', () => {
  assert.equal(shouldHandleRuntimeStreamMutation({
    streamToken: 3,
    currentToken: 3,
    streamRunId: 'run-001',
    activeRunId: 'run-001',
  }), true);

  assert.equal(shouldHandleRuntimeStreamMutation({
    streamToken: 2,
    currentToken: 3,
    streamRunId: 'run-001',
    activeRunId: 'run-001',
  }), false);

  assert.equal(shouldHandleRuntimeStreamMutation({
    streamToken: 3,
    currentToken: 3,
    streamRunId: 'run-001',
    activeRunId: 'run-002',
  }), false);

  assert.equal(shouldHandleRuntimeStreamMutation({
    streamToken: 3,
    currentToken: 3,
    streamRunId: 'run-001',
    activeRunId: '',
  }), false);
});

test('buildRuntimeAnalysisContext clears dirty trace_id after downgrade', () => {
  const context = buildRuntimeAnalysisContext({
    analysisType: 'trace',
    traceId: '   ',
    serviceName: 'query-service',
    baseContext: {
      agent_mode: 'followup_analysis_runtime',
      trace_id: 'trace-stale-123',
    },
  });

  assert.deepEqual(context, {
    agent_mode: 'followup_analysis_runtime',
    analysis_type: 'log',
    analysis_type_original: 'trace',
    analysis_type_downgraded: true,
    analysis_type_downgrade_reason: 'trace_id_missing',
    service_name: 'query-service',
  });
});

test('buildRuntimeAnalysisContext retains trace_id for trace mode when provided', () => {
  const context = buildRuntimeAnalysisContext({
    analysisType: 'trace',
    traceId: ' trace-valid-001 ',
    serviceName: 'query-service',
    baseContext: {
      agent_mode: 'followup_analysis_runtime',
    },
  });

  assert.equal(context.analysis_type, 'trace');
  assert.equal(context.trace_id, 'trace-valid-001');
  assert.equal(context.analysis_type_downgraded, undefined);
  assert.equal(context.agent_mode, 'followup_analysis_runtime');
});

test('buildRuntimeAnalysisContext retains trace_id for log mode when provided', () => {
  const context = buildRuntimeAnalysisContext({
    analysisType: 'log',
    traceId: ' trace-valid-log-001 ',
    serviceName: 'query-service',
    baseContext: {
      agent_mode: 'followup_analysis_runtime',
    },
  });

  assert.equal(context.analysis_type, 'log');
  assert.equal(context.trace_id, 'trace-valid-log-001');
  assert.equal(context.analysis_type_downgraded, undefined);
  assert.equal(context.agent_mode, 'followup_analysis_runtime');
});

test('buildRuntimeFollowUpContext carries explicit evidence window and anchor aliases', () => {
  const context = buildRuntimeFollowUpContext({
    analysisSessionId: 'sess-001',
    analysisType: 'log',
    serviceName: 'query-service',
    inputText: 'ERROR request failed',
    question: '为什么失败',
    llmInfo: { method: 'llm' },
    result: { overview: { problem: 'clickhouse_query_error' } },
    detectedTraceId: '',
    detectedRequestId: '',
    sourceLogTimestamp: '2026-04-12T13:31:14Z',
    sourceTraceId: '',
    sourceRequestId: '',
    followup_related_anchor_utc: '2026-04-12T13:31:14Z',
    followup_related_start_time: '2026-04-12T13:26:14Z',
    followup_related_end_time: '2026-04-12T13:36:14Z',
    evidence_window_start: '2026-04-12T13:25:14Z',
    evidence_window_end: '2026-04-12T13:37:14Z',
    followupRelatedMeta: {
      followup_related_request_id: 'req-123',
    },
  });

  assert.equal(context.request_id, 'req-123');
  assert.equal(context.related_log_anchor_timestamp, '2026-04-12T13:31:14Z');
  assert.equal(context.request_flow_window_start, '2026-04-12T13:26:14Z');
  assert.equal(context.request_flow_window_end, '2026-04-12T13:36:14Z');
  assert.equal(context.evidence_window_start, '2026-04-12T13:25:14Z');
  assert.equal(context.evidence_window_end, '2026-04-12T13:37:14Z');
});

test('buildRuntimeFollowUpContext prefers current inputs over stale metadata and preserves core normalized fields', () => {
  const context = buildRuntimeFollowUpContext({
    analysisSessionId: 'sess-current',
    analysisType: 'trace',
    serviceName: 'query-service',
    inputText: 'CURRENT input text',
    question: 'CURRENT question',
    llmInfo: { method: 'llm-current' },
    result: { overview: { problem: 'current-problem' } },
    detectedTraceId: 'trace-current-detected',
    detectedRequestId: 'req-current-detected',
    sourceLogTimestamp: '2026-04-12T13:31:14Z',
    sourceTraceId: 'trace-current-source',
    sourceRequestId: 'req-current-source',
    followup_related_anchor_utc: '2026-04-12T13:31:14Z',
    followup_related_start_time: '2026-04-12T13:26:14Z',
    followup_related_end_time: '2026-04-12T13:36:14Z',
    evidence_window_start: '2026-04-12T13:25:14Z',
    evidence_window_end: '2026-04-12T13:37:14Z',
    followupRelatedMeta: {
      agent_mode: 'stale-agent-mode',
      session_id: 'sess-stale',
      input_text: 'STALE input text',
      question: 'STALE question',
      llm_info: { method: 'llm-stale' },
      result: { overview: { problem: 'stale-problem' } },
      source_log_timestamp: '1999-01-01T00:00:00Z',
      source_trace_id: 'trace-stale-source',
      source_request_id: 'req-stale-source',
      trace_id: 'trace-stale-meta',
      request_id: 'req-stale-meta',
      followup_related_anchor_utc: '1999-01-01T00:01:00Z',
      followup_related_start_time: '1999-01-01T00:02:00Z',
      followup_related_end_time: '1999-01-01T00:03:00Z',
      evidence_window_start: '1999-01-01T00:04:00Z',
      evidence_window_end: '1999-01-01T00:05:00Z',
    },
  });

  assert.equal(context.agent_mode, 'request_flow');
  assert.equal(context.session_id, 'sess-current');
  assert.equal(context.input_text, 'CURRENT input text');
  assert.equal(context.question, 'CURRENT question');
  assert.deepEqual(context.llm_info, { method: 'llm-current' });
  assert.deepEqual(context.result, { overview: { problem: 'current-problem' } });
  assert.equal(context.source_log_timestamp, '2026-04-12T13:31:14Z');
  assert.equal(context.source_trace_id, 'trace-current-source');
  assert.equal(context.source_request_id, 'req-current-source');
  assert.equal(context.trace_id, 'trace-current-detected');
  assert.equal(context.request_id, 'req-current-detected');
  assert.equal(context.related_log_anchor_timestamp, '2026-04-12T13:31:14Z');
  assert.equal(context.request_flow_window_start, '2026-04-12T13:26:14Z');
  assert.equal(context.request_flow_window_end, '2026-04-12T13:36:14Z');
  assert.equal(context.evidence_window_start, '2026-04-12T13:25:14Z');
  assert.equal(context.evidence_window_end, '2026-04-12T13:37:14Z');
});

test('buildRuntimeFollowUpContext prefers explicit result window over alias inputs', () => {
  const context = buildRuntimeFollowUpContext({
    analysisSessionId: 'sess-explicit',
    analysisType: 'log',
    serviceName: 'query-service',
    inputText: 'explicit window check',
    question: 'window preference',
    result: {
      overview: { problem: 'window-mismatch' },
      request_flow_window_start: '2026-04-12T13:20:00Z',
      request_flow_window_end: '2026-04-12T13:40:00Z',
      request_id: 'req-from-result',
    },
    followupRelatedMeta: {
      followup_related_start_time: '1999-01-01T00:00:00Z',
      followup_related_end_time: '1999-01-01T00:10:00Z',
      evidence_window_start: '1999-01-01T00:01:00Z',
      evidence_window_end: '1999-01-01T00:02:00Z',
      followup_related_request_id: 'req-from-meta',
    },
  });

  assert.equal(context.request_flow_window_start, '2026-04-12T13:20:00Z');
  assert.equal(context.request_flow_window_end, '2026-04-12T13:40:00Z');
  assert.equal(context.evidence_window_start, '2026-04-12T13:20:00Z');
  assert.equal(context.evidence_window_end, '2026-04-12T13:40:00Z');
  assert.equal(context.request_id, 'req-from-result');
});

test('buildRuntimeAnalysisContext clears stale downgrade markers when trace mode later resolves cleanly', () => {
  const context = buildRuntimeAnalysisContext({
    analysisType: 'trace',
    traceId: 'trace-123',
    serviceName: 'query-service',
    baseContext: {
      agent_mode: 'followup_analysis_runtime',
      analysis_type_original: 'trace',
      analysis_type_downgraded: true,
      analysis_type_downgrade_reason: 'trace_id_missing',
    },
  });

  assert.deepEqual(context, {
    agent_mode: 'followup_analysis_runtime',
    analysis_type: 'trace',
    trace_id: 'trace-123',
    service_name: 'query-service',
  });
});

test('runtime view treats blocked as terminal status', () => {
  assert.equal(isTerminalAgentRunStatus('blocked'), true);
  assert.equal(isTerminalAgentRunStatus('completed'), true);
  assert.equal(isTerminalAgentRunStatus('running'), false);
});

test('buildRuntimeTranscriptMessage shows blocked fallback answer when no assistant final text exists', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-blocked',
        session_id: 'sess-blocked',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-blocked',
        assistant_message_id: 'msg-a-blocked',
        status: 'blocked',
        question: '危险操作是否继续',
        summary_json: { current_phase: 'blocked', blocked_reason: 'approval_rejected' },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-blocked',
    title: '危险操作是否继续',
    state,
  });
  const answerBlock = transcript.blocks.find((block) => block.type === 'answer');

  assert.equal(answerBlock?.type, 'answer');
  assert.equal(answerBlock?.content, '命令审批被拒绝，当前运行已阻塞。');
});

test('buildRuntimeTranscriptMessage shows evidence-gap blocked messaging for react replan', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-blocked-react-replan',
        session_id: 'sess-blocked-react-replan',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-blocked-react-replan',
        assistant_message_id: 'msg-a-blocked-react-replan',
        status: 'blocked',
        question: '继续排查慢查询',
        summary_json: { current_phase: 'blocked', blocked_reason: 'react_replan_needed' },
      },
    },
  });
  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        event_id: 'evt-blocked-react-replan',
        run_id: 'run-blocked-react-replan',
        seq: 1,
        event_type: 'run_status_changed',
        created_at: '2026-04-12T08:07:19.739527Z',
        payload: { status: 'blocked' },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-blocked-react-replan',
    title: '继续排查慢查询',
    state,
  });
  const answerBlock = transcript.blocks.find((block) => block.type === 'answer');
  const statusBlock = transcript.blocks.find((block) => block.type === 'status' && block.id === 'status-evt-blocked-react-replan');

  assert.equal(answerBlock?.type, 'answer');
  assert.equal(answerBlock?.content, '关键证据仍未补齐，当前已暂停自动闭环，请继续执行建议命令。');
  assert.equal(statusBlock?.type, 'status');
  assert.equal(statusBlock?.summary, '关键证据仍不足，当前已暂停自动闭环，需继续执行建议命令。');
});

test('buildRuntimeTranscriptMessage shows planning-incomplete blocked messaging', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-blocked-planning-incomplete',
        session_id: 'sess-blocked-planning-incomplete',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-blocked-planning-incomplete',
        assistant_message_id: 'msg-a-blocked-planning-incomplete',
        status: 'blocked',
        question: '继续排查慢查询',
        summary_json: { current_phase: 'blocked', blocked_reason: 'planning_incomplete' },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-blocked-planning-incomplete',
    title: '继续排查慢查询',
    state,
  });
  const answerBlock = transcript.blocks.find((block) => block.type === 'answer');

  assert.equal(answerBlock?.type, 'answer');
  assert.equal(answerBlock?.content, '当前命令计划大多不可执行，需先修复结构化命令后再继续闭环。');
});

test('buildRuntimeTranscriptMessage prefers approval waiting answer over running fallback', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-approval',
        session_id: 'sess-approval',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-approval',
        assistant_message_id: 'msg-a-approval',
        status: 'waiting_approval',
        question: '是否执行',
        summary_json: { current_phase: 'waiting_approval' },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-approval',
        seq: 1,
        event_type: 'approval_required',
        payload: {
          approval_id: 'apr-approval',
          command: 'kubectl rollout restart deploy/query-service',
          title: '重启服务',
        },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-approval',
    title: '是否执行',
    state,
  });
  const answerBlock = transcript.blocks.find((block) => block.type === 'answer');

  assert.equal(answerBlock?.type, 'answer');
  assert.equal(answerBlock?.content, '等待审批后继续执行。');
});

test('buildRuntimeTranscriptMessage renders business question for waiting user input', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-business-question',
        session_id: 'sess-business-question',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-business-question',
        assistant_message_id: 'msg-a-business-question',
        status: 'waiting_user_input',
        question: '继续排查慢查询',
        summary_json: { current_phase: 'waiting_user_input', iteration: 2 },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-business-question',
        seq: 1,
        event_type: 'action_waiting_user_input',
        payload: {
          action_id: 'act-business-question',
          kind: 'business_question',
          question_kind: 'execution_scope',
          title: '还需要你确认排查范围',
          prompt: '我还缺一个排查范围后继续。请说明先看最近 15 分钟还是 1 小时。',
          reason: '系统已先自动修正 2 轮。',
          command: 'kubectl logs --tail=100 -l app=query-service',
          purpose: '确认慢查询影响范围',
        },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-business-question',
    title: '继续排查慢查询',
    state,
  });
  const userInputBlock = transcript.blocks.find((block) => block.type === 'user_input');
  const answerBlock = transcript.blocks.find((block) => block.type === 'answer');

  assert.equal(userInputBlock?.type, 'user_input');
  assert.equal(userInputBlock?.questionKind, 'execution_scope');
  assert.equal(userInputBlock?.command, undefined);
  assert.match(userInputBlock?.prompt || '', /最近 15 分钟还是 1 小时/);
  assert.doesNotMatch(userInputBlock?.title || '', /命令语义/);
  assert.equal(answerBlock?.type, 'answer');
  assert.match(answerBlock?.content || '', /最近 15 分钟还是 1 小时/);
  assert.doesNotMatch(answerBlock?.content || '', /命令语义/);
});

test('buildRuntimeTranscriptMessage keeps only latest pending user input block in waiting_user_input state', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-user-input-latest',
        session_id: 'sess-user-input-latest',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-user-input-latest',
        assistant_message_id: 'msg-a-user-input-latest',
        status: 'waiting_user_input',
        question: '继续排查慢查询',
        summary_json: { current_phase: 'waiting_user_input', iteration: 3 },
      },
    },
  });

  [
    {
      run_id: 'run-user-input-latest',
      seq: 1,
      event_type: 'action_waiting_user_input',
      payload: {
        action_id: 'act-input-001',
        kind: 'business_question',
        question_kind: 'diagnosis_goal',
        title: '还需要你确认排查目标',
        prompt: '先确认排查目标：定位根因还是确认影响范围。',
      },
    },
    {
      run_id: 'run-user-input-latest',
      seq: 2,
      event_type: 'action_waiting_user_input',
      payload: {
        action_id: 'act-input-002',
        kind: 'business_question',
        question_kind: 'execution_scope',
        title: '还需要你确认执行范围',
        prompt: '你前一轮目标已收到，请确认先看最近 15 分钟还是 1 小时。',
      },
    },
  ].forEach((event) => {
    state = agentRunReducer(state, {
      type: 'append_event',
      payload: { event },
    });
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-user-input-latest',
    title: '继续排查慢查询',
    state,
  });
  const userInputBlocks = transcript.blocks.filter((block) => block.type === 'user_input');

  assert.equal(userInputBlocks.length, 1);
  assert.equal(userInputBlocks[0]?.type, 'user_input');
  assert.equal(userInputBlocks[0]?.title, '还需要你确认执行范围');
  assert.match(userInputBlocks[0]?.prompt || '', /最近 15 分钟还是 1 小时/);
});

test('buildRuntimeTranscriptMessage does not render stale user input block when run is not waiting_user_input', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-user-input-stale',
        session_id: 'sess-user-input-stale',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-user-input-stale',
        assistant_message_id: 'msg-a-user-input-stale',
        status: 'completed',
        question: '继续排查慢查询',
        summary_json: { current_phase: 'completed', iteration: 3 },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-user-input-stale',
        seq: 1,
        event_type: 'action_waiting_user_input',
        payload: {
          action_id: 'act-input-stale',
          kind: 'business_question',
          question_kind: 'diagnosis_goal',
          title: '还需要你确认排查目标',
          prompt: '请确认优先目标。',
        },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-user-input-stale',
    title: '继续排查慢查询',
    state,
  });
  assert.equal(
    transcript.blocks.some((block) => block.type === 'user_input'),
    false,
  );
});

test('buildRuntimeTranscriptMessage renders template hint block from replan items', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-template-hint',
        session_id: 'sess-template-hint',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-template-hint',
        assistant_message_id: 'msg-a-template-hint',
        status: 'completed',
        question: '继续排查',
        summary_json: { current_phase: 'completed', iteration: 2 },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-template-hint',
        seq: 1,
        event_type: 'message_started',
        payload: { assistant_message_id: 'msg-a-template-hint' },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-template-hint',
        seq: 2,
        event_type: 'assistant_message_finalized',
        payload: {
          assistant_message_id: 'msg-a-template-hint',
          content: '当前计划没有可自动执行的结构化查询命令。',
          metadata: {
            react_loop: {
              replan: {
                needed: true,
                items: [
                  {
                    reason: 'command_template_suggested',
                    title: '建议补全命令模板',
                    summary: '补全结构化命令后执行：kubectl -n islap get pods -l app=query-service',
                    suggested_command: 'kubectl -n islap get pods -l app=query-service',
                    suggested_command_spec: {
                      tool: 'generic_exec',
                      args: {
                        command: 'kubectl -n islap get pods -l app=query-service',
                        target_kind: 'k8s_cluster',
                        target_identity: 'namespace:islap',
                      },
                    },
                  },
                ],
              },
            },
          },
        },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-template-hint',
    title: '继续排查',
    state,
  });
  const templateHint = transcript.blocks.find((block) => block.type === 'template_hint');

  assert.equal(templateHint?.type, 'template_hint');
  assert.equal(templateHint?.reason, 'command_template_suggested');
  assert.equal(templateHint?.suggestedCommand, 'kubectl -n islap get pods -l app=query-service');
  assert.equal(templateHint?.runId, 'run-template-hint');
  assert.equal(typeof templateHint?.suggestedCommandSpec, 'object');
});

test('buildRuntimePresentation keeps conversation blocks and sinks detail blocks', () => {
  const presentation = buildRuntimePresentation({
    runId: 'run-presentation',
    title: '继续排查慢查询',
    status: 'running',
    currentPhase: 'planning',
    updatedAt: '2026-03-26T10:00:00Z',
    blocks: [
      {
        id: 'thinking-1',
        type: 'thinking',
        title: '加载上下文历史',
        status: 'info',
      },
      {
        id: 'command-1',
        type: 'command',
        title: '执行命令',
        command: 'kubectl logs -n islap -l app=query-service',
        status: 'running',
      },
      {
        id: 'approval-1',
        type: 'approval',
        approval: {
          id: 'apr-1',
          runtimeRunId: 'run-presentation',
          runtimeApprovalId: 'apr-1',
          title: '审批动作',
          command: 'kubectl rollout restart deploy/query-service',
          status: 'pending',
        },
      },
      {
        id: 'answer-1',
        type: 'answer',
        content: '已知事实 / 未知缺口 / 下一步动作',
        finalized: true,
        streaming: false,
      },
    ],
  });

  assert.deepEqual(
    presentation.conversation.blocks.map((block) => block.type),
    ['approval', 'answer'],
  );
  assert.deepEqual(
    presentation.detailBlocks.map((block) => block.type),
    ['thinking', 'command'],
  );
  assert.equal(presentation.hasDetails, true);
});

test('buildRuntimePresentation promotes pre-command plan thinking block to conversation', () => {
  const presentation = buildRuntimePresentation({
    runId: 'run-pre-command-plan',
    title: '继续排查慢查询',
    status: 'running',
    currentPhase: 'action',
    updatedAt: '2026-03-26T10:00:00Z',
    blocks: [
      {
        id: 'thinking-plan-1',
        type: 'thinking',
        title: '执行前计划',
        phase: 'action',
        status: 'info',
        detail: '执行前计划：\n1. 计划命令：kubectl logs -n islap -l app=query-service',
      },
      {
        id: 'thinking-2',
        type: 'thinking',
        title: '加载上下文历史',
        status: 'info',
      },
      {
        id: 'answer-1',
        type: 'answer',
        content: '继续执行中',
        finalized: false,
        streaming: true,
      },
    ],
  });

  assert.deepEqual(
    presentation.conversation.blocks.map((block) => block.type),
    ['thinking', 'answer'],
  );
  assert.deepEqual(
    presentation.detailBlocks.map((block) => `${block.type}:${String(block.title || '')}`),
    ['thinking:加载上下文历史'],
  );
});

test('buildRuntimePresentation keeps template hint blocks in conversation', () => {
  const presentation = buildRuntimePresentation({
    runId: 'run-template-conversation',
    title: '继续排查',
    status: 'completed',
    blocks: [
      {
        id: 'template-1',
        type: 'template_hint',
        runId: 'run-template-conversation',
        title: '建议补全命令模板',
        suggestedCommand: 'kubectl -n islap get pods -l app=query-service',
      },
      {
        id: 'answer-1',
        type: 'answer',
        content: '请使用模板补齐后继续执行。',
        finalized: true,
        streaming: false,
      },
    ],
  });

  assert.deepEqual(
    presentation.conversation.blocks.map((block) => block.type),
    ['template_hint', 'answer'],
  );
  assert.deepEqual(
    presentation.detailBlocks.map((block) => block.type),
    [],
  );
});

test('buildRuntimeTranscriptMessage renders diagnosis summary and next best commands from run summary', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-diagnosis-summary',
        session_id: 'sess-diagnosis-summary',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-diagnosis-summary',
        assistant_message_id: 'msg-a-diagnosis-summary',
        status: 'blocked',
        question: '继续排查慢查询',
        summary_json: {
          current_phase: 'blocked',
          diagnosis_status: 'blocked',
          fault_summary: 'query-service 查询 system.tables 出现慢查询',
          plan_quality: {
            planning_blocked_reason: '当前命令计划中有 3/4 条动作仍不可执行，应先修复结构化命令再继续闭环。',
          },
          plan_coverage: 0.5,
          exec_coverage: 1.0,
          evidence_coverage: 0.5,
          final_confidence: 0.46,
          missing_evidence_slots: ['slot_resource_pressure'],
          next_best_commands: [
            {
              slot_id: 'slot_resource_pressure',
              command: "kubectl -n islap exec deploy/clickhouse -- clickhouse-client --query 'SELECT 1'",
              why: '补齐资源侧证据',
              expected_signal: '确认 CPU/并发压力',
            },
            {
              slot_id: 'slot_query_log_detail',
              command: "kubectl -n islap logs -l app=query-service --since=20m | rg 'CH_QUERY_SLOW'",
              why: '补齐慢查询上下文',
              expected_signal: '确认慢查询触发频次',
            },
          ],
        },
      },
    },
  });
  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        event_id: 'evt-diagnosis-summary-assistant',
        run_id: 'run-diagnosis-summary',
        seq: 1,
        event_type: 'assistant_message_finalized',
        created_at: '2026-04-12T08:07:19.673140Z',
        payload: {
          assistant_message_id: 'msg-a-diagnosis-summary',
          content: '继续排查中。',
          metadata: {
            actions: [
              {
                id: 'tmpl-window-001',
                evidence_window_start: '2026-04-11T12:58:33Z',
                evidence_window_end: '2026-04-11T13:08:33Z',
              },
            ],
          },
        },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-diagnosis-summary',
    title: '继续排查慢查询',
    state,
  });
  const diagnosisBlock = transcript.blocks.find((block) => block.type === 'status' && block.id === 'diagnosis-status');
  const recommendedCommands = transcript.blocks.filter(
    (block) => block.type === 'command' && String(block.id || '').startsWith('recommended-command-'),
  );

  assert.equal(diagnosisBlock?.type, 'status');
  assert.match(diagnosisBlock?.summary || '', /故障总结/);
  assert.match(diagnosisBlock?.summary || '', /计划质量/);
  assert.match(diagnosisBlock?.summary || '', /evidence=0.5/);
  assert.match(diagnosisBlock?.summary || '', /证据时间窗：2026-04-11T12:58:33Z ~ 2026-04-11T13:08:33Z/);
  assert.equal(recommendedCommands.length, 2);
  assert.match(recommendedCommands[0]?.command || '', /clickhouse-client/);

  const presentation = buildRuntimePresentation(transcript);
  assert.equal(
    presentation.conversation.blocks.some((block) => block.id === 'diagnosis-status'),
    true,
  );
  assert.equal(
    presentation.conversation.blocks.some((block) => String(block.id || '').startsWith('recommended-command-')),
    true,
  );
});

test('buildRuntimeTranscriptMessage exposes resolved target context on command blocks', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-target-context',
        session_id: 'sess-target-context',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-target-context',
        assistant_message_id: 'msg-a-target-context',
        status: 'running',
        question: '排查节点级异常',
        summary_json: { current_phase: 'action', iteration: 1 },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-target-context',
        seq: 1,
        event_type: 'tool_call_started',
        payload: {
          tool_call_id: 'tool-target-context-001',
          command_run_id: 'cmdrun-target-context-001',
          command: 'kubectl -n islap logs pod/query-service',
          status: 'running',
          target_kind: 'host_node',
          target_identity: 'node:worker-01',
          target_cluster_id: 'cluster-dev',
          target_namespace: 'islap',
          target_node_name: 'worker-01',
          resolved_target_context: {
            execution_scope: {
              cluster_id: 'cluster-dev',
              namespace: 'islap',
              node_name: 'worker-01',
            },
          },
        },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-target-context',
    title: '排查节点级异常',
    state,
  });
  const commandBlock = transcript.blocks.find((block) => block.type === 'command');
  assert.equal(commandBlock?.type, 'command');
  assert.equal(commandBlock?.targetClusterId, 'cluster-dev');
  assert.equal(commandBlock?.targetNamespace, 'islap');
  assert.equal(commandBlock?.targetNodeName, 'worker-01');
  assert.deepEqual(commandBlock?.resolvedTargetContext, {
    execution_scope: {
      cluster_id: 'cluster-dev',
      namespace: 'islap',
      node_name: 'worker-01',
    },
  });
});

test('buildRuntimeTranscriptMessage keeps pending manual action visible when executable=false', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-manual',
        session_id: 'sess-manual',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-manual',
        assistant_message_id: 'msg-a-manual',
        status: 'waiting_user_input',
        question: '继续排查超时',
        summary_json: { current_phase: 'waiting_user_input', iteration: 2 },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-manual',
        seq: 1,
        event_type: 'message_started',
        payload: { assistant_message_id: 'msg-a-manual' },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-manual',
        seq: 2,
        event_type: 'assistant_message_finalized',
        payload: {
          assistant_message_id: 'msg-a-manual',
          content: '需要补充排查步骤。',
          metadata: {
            actions: [
              {
                id: 'rf-1',
                title: '补充 ERROR 日志后再确认根因优先级',
                action_type: 'manual',
                command: 'kubectl -n islap get pods -l app=query-service',
                command_type: 'query',
                executable: false,
                reason: '模型建议先人工确认后再执行只读命令',
              },
            ],
            react_loop: {
              replan: {
                needed: true,
                next_actions: ['存在 unknown 类型动作，需人工确认当前步骤后再执行。'],
              },
            },
          },
        },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-manual',
    title: '继续排查超时',
    state,
  });
  const manualBlock = transcript.blocks.find((block) => block.type === 'manual_action');

  assert.equal(manualBlock?.type, 'manual_action');
  assert.equal(manualBlock?.action.title, '补充 ERROR 日志后再确认根因优先级');
  assert.equal(manualBlock?.action.command, 'kubectl -n islap get pods -l app=query-service');
  assert.match(
    manualBlock?.action.message || '',
    /人工确认后再执行只读命令|确认后再执行/,
  );
});

test('buildRuntimeTranscriptMessage does not use action.question as manual action title fallback', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-manual-question-fallback',
        session_id: 'sess-manual-question-fallback',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-manual-question-fallback',
        assistant_message_id: 'msg-a-manual-question-fallback',
        status: 'waiting_user_input',
        question: '继续排查超时',
        summary_json: { current_phase: 'waiting_user_input', iteration: 2 },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-manual-question-fallback',
        seq: 1,
        event_type: 'assistant_message_finalized',
        payload: {
          assistant_message_id: 'msg-a-manual-question-fallback',
          content: '需要补充排查步骤。',
          metadata: {
            actions: [
              {
                id: 'rf-2',
                title: '',
                question: '这是用户原问题，不应作为动作标题',
                action_type: 'manual',
                command: 'kubectl -n islap get pods -l app=query-service',
                command_type: 'query',
                executable: false,
                reason: '缺少结构化命令参数',
              },
            ],
          },
        },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-manual-question-fallback',
    title: '继续排查超时',
    state,
  });
  const manualBlock = transcript.blocks.find((block) => block.type === 'manual_action');

  assert.equal(manualBlock?.type, 'manual_action');
  assert.equal(manualBlock?.action.title, 'kubectl -n islap get pods -l app=query-service');
});

test('buildRuntimeTranscriptMessage suppresses planning blocks when suppression is enabled', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-planning-suppressed',
        session_id: 'sess-planning-suppressed',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-planning-suppressed',
        assistant_message_id: 'msg-a-planning-suppressed',
        status: 'running',
        question: '继续排查慢查询',
        summary_json: { current_phase: 'planning', iteration: 1 },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-planning-suppressed',
        seq: 1,
        event_type: 'reasoning_step',
        payload: {
          phase: 'planning',
          title: '加载上下文历史',
          status: 'info',
          detail: '准备读取同会话历史',
          iteration: 1,
        },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-planning-suppressed',
        seq: 2,
        event_type: 'reasoning_step',
        payload: {
          phase: 'reasoning',
          title: '识别关键错误码',
          status: 'info',
          detail: '定位 timeout 相关日志',
          iteration: 1,
        },
      },
    },
  });

  const transcriptWithoutSuppression = buildRuntimeTranscriptMessage({
    runId: 'run-planning-suppressed',
    title: '继续排查慢查询',
    state,
    suppressBoilerplatePlanning: false,
  });
  const transcriptWithSuppression = buildRuntimeTranscriptMessage({
    runId: 'run-planning-suppressed',
    title: '继续排查慢查询',
    state,
    suppressBoilerplatePlanning: true,
  });

  assert.equal(
    transcriptWithoutSuppression.blocks.some((block) => block.type === 'thinking' && block.phase === 'planning'),
    true,
  );
  assert.equal(
    transcriptWithSuppression.blocks.some((block) => block.type === 'thinking' && block.phase === 'planning'),
    false,
  );
  assert.equal(
    transcriptWithSuppression.blocks.some((block) => block.type === 'thinking' && block.phase === 'reasoning'),
    true,
  );
});

test('buildRuntimeTranscriptMessage dedupes bootstrap planning blocks across phase and iteration drift', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-planning-dedupe',
        session_id: 'sess-planning-dedupe',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-planning-dedupe',
        assistant_message_id: 'msg-a-planning-dedupe',
        status: 'running',
        question: '继续排查慢查询',
        summary_json: { current_phase: 'planning', iteration: 1 },
      },
    },
  });

  [
    {
      run_id: 'run-planning-dedupe',
      seq: 1,
      event_type: 'reasoning_step',
      payload: {
        phase: 'planning',
        title: '加载上下文历史',
        status: 'info',
        detail: '准备读取同会话历史',
      },
    },
    {
      run_id: 'run-planning-dedupe',
      seq: 2,
      event_type: 'reasoning_step',
      payload: {
        phase: 'plan',
        title: '加载上下文历史',
        status: 'info',
        detail: '准备读取同会话历史',
        iteration: 0,
      },
    },
    {
      run_id: 'run-planning-dedupe',
      seq: 3,
      event_type: 'reasoning_step',
      payload: {
        phase: 'planning',
        title: '加载上下文历史',
        status: 'info',
        detail: '准备读取同会话历史',
        iteration: 2,
      },
    },
  ].forEach((event) => {
    state = agentRunReducer(state, {
      type: 'append_event',
      payload: { event },
    });
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-planning-dedupe',
    title: '继续排查慢查询',
    state,
    suppressBoilerplatePlanning: false,
  });

  const planningBlocks = transcript.blocks.filter((block) => (
    block.type === 'thinking'
    && (block.phase === 'planning' || block.phase === 'plan')
    && block.title === '加载上下文历史'
  ));
  assert.equal(planningBlocks.length, 1);
});

test('buildRuntimeTranscriptMessage renders unified timeout message for timed_out or exit -9', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-timeout',
        session_id: 'sess-timeout',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-timeout',
        assistant_message_id: 'msg-a-timeout',
        status: 'running',
        question: '执行 DDL 查询',
        summary_json: { current_phase: 'action', iteration: 1 },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-timeout',
        seq: 1,
        event_type: 'tool_call_finished',
        payload: {
          tool_call_id: 'tool-timeout-001',
          command_run_id: 'cmdrun-timeout-001',
          command: 'kubectl -n islap exec ... -- clickhouse-client --query "SHOW CREATE TABLE logs.traces"',
          status: 'failed',
          timed_out: true,
          exit_code: -9,
          stderr: '',
          stdout: '',
        },
      },
    },
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-timeout',
    title: '执行 DDL 查询',
    state,
  });

  const commandBlock = transcript.blocks.find((block) => block.type === 'command');
  assert.equal(commandBlock?.type, 'command');
  assert.equal(
    commandBlock?.message,
    '命令超时终止（timed_out/exit -9）。建议先缩小查询范围（时间窗口/limit），再提高 timeout 重试。',
  );
});

test('agentRunReducer builds assistant message and command output from events', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-001',
        session_id: 'sess-001',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-001',
        assistant_message_id: 'msg-a-001',
        status: 'running',
        question: '排查超时',
        summary_json: { current_phase: 'planning', iteration: 0 },
      },
    },
  });

  const events = [
    {
      run_id: 'run-001',
      seq: 1,
      event_type: 'message_started',
      payload: { assistant_message_id: 'msg-a-001' },
    },
    {
      run_id: 'run-001',
      seq: 2,
      event_type: 'assistant_delta',
      payload: { assistant_message_id: 'msg-a-001', text: '第一段' },
    },
    {
      run_id: 'run-001',
      seq: 3,
      event_type: 'tool_call_started',
      payload: {
        tool_call_id: 'tool-001',
        tool_name: 'command.exec',
        status: 'running',
        command_run_id: 'cmdrun-001',
      },
    },
    {
      run_id: 'run-001',
      seq: 4,
      event_type: 'tool_call_output_delta',
      payload: {
        tool_call_id: 'tool-001',
        command_run_id: 'cmdrun-001',
        stream: 'stdout',
        text: 'line-1\n',
      },
    },
    {
      run_id: 'run-001',
      seq: 5,
      event_type: 'tool_call_finished',
      payload: {
        tool_call_id: 'tool-001',
        command_run_id: 'cmdrun-001',
        status: 'completed',
        stdout: 'line-1\n',
        exit_code: 0,
      },
    },
    {
      run_id: 'run-001',
      seq: 6,
      event_type: 'assistant_message_finalized',
      payload: { assistant_message_id: 'msg-a-001', content: '第一段\n结论' },
    },
  ];

  state = agentRunReducer(state, {
    type: 'hydrate_events',
    payload: { events },
  });

  assert.equal(selectAssistantMessage(state)?.content, '第一段\n结论');
  assert.equal(selectAssistantMessage(state)?.finalized, true);
  assert.deepEqual(selectCommandRuns(state).map((item) => item.stdout), ['line-1\n']);
  assert.deepEqual(selectCommandRuns(state).map((item) => item.status), ['completed']);
});

test('agentRunReducer prefers explicit command_run_id when tool_call_id is reused', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-explicit-command-run-id',
        session_id: 'sess-explicit-command-run-id',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-explicit-command-run-id',
        assistant_message_id: 'msg-a-explicit-command-run-id',
        status: 'running',
        question: '排查重复命令关联',
        summary_json: { current_phase: 'action', iteration: 1 },
      },
    },
  });

  [
    {
      run_id: 'run-explicit-command-run-id',
      seq: 1,
      event_type: 'tool_call_started',
      payload: {
        tool_call_id: 'tool-shared-001',
        command_run_id: 'cmdrun-001',
        status: 'running',
        command: 'echo one',
      },
    },
    {
      run_id: 'run-explicit-command-run-id',
      seq: 2,
      event_type: 'tool_call_finished',
      payload: {
        tool_call_id: 'tool-shared-001',
        command_run_id: 'cmdrun-001',
        status: 'completed',
        command: 'echo one',
        stdout: 'one\n',
      },
    },
    {
      run_id: 'run-explicit-command-run-id',
      seq: 3,
      event_type: 'tool_call_started',
      payload: {
        tool_call_id: 'tool-shared-001',
        command_run_id: 'cmdrun-002',
        status: 'running',
        command: 'echo two',
      },
    },
    {
      run_id: 'run-explicit-command-run-id',
      seq: 4,
      event_type: 'tool_call_finished',
      payload: {
        tool_call_id: 'tool-shared-001',
        command_run_id: 'cmdrun-002',
        status: 'completed',
        command: 'echo two',
        stdout: 'two\n',
      },
    },
  ].forEach((event) => {
    state = agentRunReducer(state, {
      type: 'append_event',
      payload: { event },
    });
  });

  const commandRuns = selectCommandRuns(state);
  assert.equal(commandRuns.length, 2);
  assert.deepEqual(commandRuns.map((item) => item.commandRunId), ['cmdrun-001', 'cmdrun-002']);
  assert.deepEqual(commandRuns.map((item) => item.stdout), ['one\n', 'two\n']);
});

test('agentRunReducer keeps streamed output when terminal payload is truncated', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-truncated-preserve',
        session_id: 'sess-truncated-preserve',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-truncated-preserve',
        assistant_message_id: 'msg-a-truncated-preserve',
        status: 'running',
        question: '检查输出截断保真',
        summary_json: { current_phase: 'action', iteration: 1 },
      },
    },
  });

  [
    {
      run_id: 'run-truncated-preserve',
      seq: 1,
      event_type: 'tool_call_started',
      payload: {
        tool_call_id: 'tool-truncated-001',
        command_run_id: 'cmdrun-truncated-001',
        status: 'running',
        command: 'kubectl logs ...',
      },
    },
    {
      run_id: 'run-truncated-preserve',
      seq: 2,
      event_type: 'tool_call_output_delta',
      payload: {
        tool_call_id: 'tool-truncated-001',
        command_run_id: 'cmdrun-truncated-001',
        stream: 'stdout',
        text: 'line-1\nline-2\nline-3\n',
      },
    },
    {
      run_id: 'run-truncated-preserve',
      seq: 3,
      event_type: 'tool_call_finished',
      payload: {
        tool_call_id: 'tool-truncated-001',
        command_run_id: 'cmdrun-truncated-001',
        status: 'completed',
        output_truncated: true,
        stdout: 'line-1\n',
      },
    },
  ].forEach((event) => {
    state = agentRunReducer(state, {
      type: 'append_event',
      payload: { event },
    });
  });

  const commandRuns = selectCommandRuns(state);
  assert.equal(commandRuns.length, 1);
  assert.equal(commandRuns[0].stdout, 'line-1\nline-2\nline-3\n');
  assert.equal(commandRuns[0].outputTruncated, true);
});

test('buildRuntimeTranscriptMessage merges command deltas into a single command block', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-transcript-merge',
        session_id: 'sess-transcript-merge',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-transcript-merge',
        assistant_message_id: 'msg-a-transcript-merge',
        status: 'running',
        question: '检查 transcript 命令聚合',
        summary_json: { current_phase: 'action', iteration: 1 },
      },
    },
  });

  [
    {
      run_id: 'run-transcript-merge',
      seq: 1,
      event_type: 'tool_call_started',
      payload: {
        tool_call_id: 'tool-transcript-001',
        command_run_id: 'cmdrun-transcript-001',
        status: 'running',
        command: 'kubectl logs ...',
      },
    },
    {
      run_id: 'run-transcript-merge',
      seq: 2,
      event_type: 'tool_call_output_delta',
      payload: {
        tool_call_id: 'tool-transcript-001',
        command_run_id: 'cmdrun-transcript-001',
        stream: 'stdout',
        text: 'part-1\n',
      },
    },
    {
      run_id: 'run-transcript-merge',
      seq: 3,
      event_type: 'tool_call_output_delta',
      payload: {
        tool_call_id: 'tool-transcript-001',
        command_run_id: 'cmdrun-transcript-001',
        stream: 'stdout',
        text: 'part-2\n',
      },
    },
    {
      run_id: 'run-transcript-merge',
      seq: 4,
      event_type: 'tool_call_finished',
      payload: {
        tool_call_id: 'tool-transcript-001',
        command_run_id: 'cmdrun-transcript-001',
        status: 'completed',
        output_truncated: true,
        stdout: 'part-1\n',
      },
    },
  ].forEach((event) => {
    state = agentRunReducer(state, {
      type: 'append_event',
      payload: { event },
    });
  });

  const transcript = buildRuntimeTranscriptMessage({
    runId: 'run-transcript-merge',
    title: '检查 transcript 命令聚合',
    state,
  });
  const commandBlocks = transcript.blocks.filter((block) => block.type === 'command');
  assert.equal(commandBlocks.length, 1);
  assert.equal(commandBlocks[0].stdout, 'part-1\npart-2\n');
});

test('agentRunReducer tracks pending approvals and resolution', () => {
  let state = agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: {
      run: {
        run_id: 'run-002',
        session_id: 'sess-002',
        analysis_type: 'log',
        engine: 'agent-runtime-v1',
        runtime_version: 'v1',
        user_message_id: 'msg-u-002',
        assistant_message_id: 'msg-a-002',
        status: 'waiting_approval',
        question: '执行修复',
        summary_json: { current_phase: 'waiting_approval', iteration: 1 },
      },
    },
  });

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-002',
        seq: 1,
        event_type: 'approval_required',
        payload: {
          approval_id: 'apr-001',
          command: 'kubectl rollout restart deploy/query-service',
          requires_confirmation: true,
          requires_elevation: true,
          title: '重启服务',
        },
      },
    },
  });

  assert.equal(selectPendingApprovals(state).length, 1);

  state = agentRunReducer(state, {
    type: 'append_event',
    payload: {
      event: {
        run_id: 'run-002',
        seq: 2,
        event_type: 'approval_resolved',
        payload: {
          approval_id: 'apr-001',
          decision: 'approved',
          comment: '允许继续',
        },
      },
    },
  });

  assert.equal(selectPendingApprovals(state).length, 0);
});
