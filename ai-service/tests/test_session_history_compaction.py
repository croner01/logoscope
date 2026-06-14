"""
session_history 数据压缩截断测试

验证 _compact_action_observation、_compact_followup_action
的截断阈值从 280/320 放大到 2000 后，中间尺寸数据不再被截断。
"""
from ai.session_history import (
    MESSAGE_METADATA_MAX_CHARS,
    AISessionStore,
    AISessionMessage,
    _compact_action_observation,
    _compact_followup_action,
)


# ─── _compact_action_observation 单元测试 ───


def test_compact_action_observation_preserves_medium_stdout():
    """280 < 500 字符的 stdout 不应被截断 (旧阈值 280, 新阈值 2000)。"""
    medium = "x" * 500
    result = _compact_action_observation({"stdout": medium})
    preview = result.get("stdout_preview", "")
    assert len(preview) == 500, f"expected 500 chars, got {len(preview)}"
    assert "<truncated>" not in preview


def test_compact_action_observation_preserves_medium_stderr():
    """280 < 1500 字符的 stderr 不应被截断。"""
    medium = "y" * 1500
    result = _compact_action_observation({"stderr": medium})
    preview = result.get("stderr_preview", "")
    assert len(preview) == 1500, f"expected 1500 chars, got {len(preview)}"
    assert "<truncated>" not in preview


def test_compact_action_observation_preserves_medium_command():
    """320 < 1800 字符的 command 不应被截断 (旧阈值 320)。"""
    medium = "z" * 1800
    result = _compact_action_observation({"command": medium})
    cmd = result.get("command", "")
    assert len(cmd) == 1800, f"expected 1800 chars, got {len(cmd)}"
    assert "<truncated>" not in cmd


def test_compact_action_observation_truncates_oversized_stdout():
    """> 2000 字符的 stdout 仍应被截断并标记 <truncated>。"""
    oversized = "a" * 3000
    result = _compact_action_observation({"stdout": oversized})
    preview = result.get("stdout_preview", "")
    assert len(preview) <= 2000 + 20  # 允许少量余量
    assert "<truncated>" in preview


def test_compact_action_observation_truncates_oversized_message():
    """> 2000 字符的 message 仍应被截断。"""
    oversized = "b" * 5000
    result = _compact_action_observation({"message": oversized})
    msg = result.get("message", "")
    assert len(msg) <= 2000 + 20
    assert "<truncated>" in msg


def test_compact_action_observation_preserves_exit_code():
    """exit_code 等非字符串字段保持不变（回归检查）。"""
    result = _compact_action_observation({
        "status": "completed",
        "exit_code": 0,
        "timed_out": False,
        "output_truncated": True,
    })
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["output_truncated"] is True


def test_compact_action_observation_empty_input():
    """空输入应返回默认值，不抛异常。"""
    result = _compact_action_observation(None)
    assert result["status"] == ""
    assert result["exit_code"] == 0
    assert result["stdout_preview"] == ""


def test_compact_action_observation_all_fields_at_limit():
    """所有文本字段同时填充 2000 字符不应被截断。"""
    text = "c" * 2000
    result = _compact_action_observation({
        "stdout": text,
        "stderr": text,
        "command": text,
        "message": text,
    })
    assert len(result["stdout_preview"]) == 2000
    assert len(result["stderr_preview"]) == 2000
    assert len(result["command"]) == 2000
    assert len(result["message"]) == 2000
    for val in [result["stdout_preview"], result["stderr_preview"], result["command"], result["message"]]:
        assert "<truncated>" not in val


# ─── _compact_followup_action 单元测试 ───


def test_compact_followup_action_preserves_medium_title():
    """240 < 1500 字符的 title 不应被截断 (旧阈值 240)。"""
    medium = "d" * 1500
    result = _compact_followup_action({"title": medium})
    assert len(result["title"]) == 1500
    assert "<truncated>" not in result["title"]


def test_compact_followup_action_preserves_medium_purpose():
    """280 < 1500 字符的 purpose 不应被截断 (旧阈值 280)。"""
    medium = "e" * 1500
    result = _compact_followup_action({"purpose": medium})
    assert len(result["purpose"]) == 1500
    assert "<truncated>" not in result["purpose"]


def test_compact_followup_action_preserves_medium_command():
    """320 < 1800 字符的 command 不应被截断 (旧阈值 320)。"""
    medium = "f" * 1800
    result = _compact_followup_action({"command": medium})
    assert len(result["command"]) == 1800
    assert "<truncated>" not in result["command"]


def test_compact_followup_action_truncates_oversized():
    """Oversized fields > 2000 chars should still be truncated."""
    oversized = "g" * 3000
    result = _compact_followup_action({"title": oversized, "purpose": oversized, "command": oversized})
    for key in ["title", "purpose", "command"]:
        assert len(result[key]) <= 2000 + 20
        assert "<truncated>" in result[key]


def test_compact_followup_action_preserves_bool_and_int():
    """布尔/数字字段保持不变。"""
    result = _compact_followup_action({
        "requires_confirmation": True,
        "requires_elevation": False,
        "executable": True,
        "priority": 5,
    })
    assert result["requires_confirmation"] is True
    assert result["requires_elevation"] is False
    assert result["executable"] is True
    assert result["priority"] == 5


# ─── 集成测试：经 _build_message_rows 压缩后数据完整性 ───


def test_message_compaction_keeps_medium_data_intact():
    """500 字符的字段通过完整压缩链路后应保持完整。"""
    medium = "h" * 500
    store = AISessionStore(storage_adapter=None)
    rows = store._build_message_rows(
        [
            AISessionMessage(
                session_id="ais-compact-1",
                message_id="msg-1",
                msg_index=1,
                role="assistant",
                content="answer",
                metadata={
                    "action_observations": [
                        {
                            "stdout": medium,
                            "stderr": medium,
                            "command": medium,
                            "message": medium,
                            "status": "completed",
                            "exit_code": 0,
                        }
                    ],
                    "actions": [
                        {
                            "title": medium,
                            "purpose": medium,
                            "command": medium,
                            "executable": True,
                        }
                    ],
                },
                created_at="2026-06-14T00:00:00Z",
            )
        ]
    )
    meta = rows[0]["metadata_json"]
    assert isinstance(meta, str)
    # 500 字符的字段不应触发截断，所以 metadata_json 中不应有 truncation 标记
    assert "<truncated>" not in meta, (
        "medium-sized fields should not be truncated through the full pipeline"
    )
    # 验证 stdout_preview 被正确重命名且保留完整内容
    assert f'"stdout_preview":"{"h" * 500}"' in meta or f'"stdout_preview": "{"h" * 500}"' in meta


def test_message_compaction_still_bounds_extreme_data():
    """极端大数据仍受 MESSAGE_METADATA_MAX_CHARS 限制（回归检查）。"""
    oversized = "i" * 50000
    store = AISessionStore(storage_adapter=None)
    rows = store._build_message_rows(
        [
            AISessionMessage(
                session_id="ais-compact-2",
                message_id="msg-2",
                msg_index=1,
                role="assistant",
                content="answer",
                metadata={
                    "action_observations": [
                        {
                            "stdout": oversized,
                            "stderr": oversized,
                            "command": oversized,
                            "message": oversized,
                        }
                    ],
                    "actions": [
                        {"title": oversized, "purpose": oversized, "command": oversized}
                    ],
                    "thoughts": [
                        {"phase": "plan", "title": oversized, "detail": oversized}
                    ],
                    "reflection": {"summary": oversized},
                    "react_loop": {"summary": oversized},
                },
                created_at="2026-06-14T00:00:00Z",
            )
        ]
    )
    metadata_json = rows[0]["metadata_json"]
    assert isinstance(metadata_json, str)
    assert len(metadata_json) <= MESSAGE_METADATA_MAX_CHARS + 256
    # 超大字段应该会触发截断
    assert "<truncated>" in metadata_json
    assert "stdout_preview" in metadata_json
    assert "\"stdout\":" not in metadata_json


def test_message_compaction_multiple_observations_all_preserved():
    """多条 action_observation 不应被静默丢弃。"""
    store = AISessionStore(storage_adapter=None)
    rows = store._build_message_rows(
        [
            AISessionMessage(
                session_id="ais-compact-3",
                message_id="msg-3",
                msg_index=1,
                role="assistant",
                content="answer",
                metadata={
                    "action_observations": [
                        {"command": f"cmd-{i}", "status": "completed", "exit_code": 0}
                        for i in range(5)
                    ],
                },
                created_at="2026-06-14T00:00:00Z",
            )
        ]
    )
    meta = rows[0]["metadata_json"]
    for i in range(5):
        assert f"cmd-{i}" in meta, f"cmd-{i} should be preserved in metadata"


def test_message_compaction_thought_detail_preserved():
    """thought detail < 2000 不应被截断。"""
    detail = "j" * 1500
    store = AISessionStore(storage_adapter=None)
    rows = store._build_message_rows(
        [
            AISessionMessage(
                session_id="ais-compact-4",
                message_id="msg-4",
                msg_index=1,
                role="assistant",
                content="answer",
                metadata={
                    "thoughts": [
                        {"phase": "thought", "title": "analyzing", "detail": detail},
                    ],
                },
                created_at="2026-06-14T00:00:00Z",
            )
        ]
    )
    meta = rows[0]["metadata_json"]
    assert detail in meta, "1500-char thought detail should be preserved"


def test_message_compaction_long_term_memory_summary_preserved():
    """LTM summary 800 字符以内的保留不受影响。"""
    summary = "k" * 800
    store = AISessionStore(storage_adapter=None)
    rows = store._build_message_rows(
        [
            AISessionMessage(
                session_id="ais-compact-5",
                message_id="msg-5",
                msg_index=1,
                role="assistant",
                content="answer",
                metadata={
                    "long_term_memory_summary": summary,
                },
                created_at="2026-06-14T00:00:00Z",
            )
        ]
    )
    meta = rows[0]["metadata_json"]
    assert summary in meta, "800-char LTM summary should be preserved"
