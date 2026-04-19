"""Output feedback and result interpretation skills."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class CommandRiskLevel(str):
    """Command risk classification."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# Dangerous patterns that require approval
DANGEROUS_PATTERNS = [
    # File operations
    (r"rm\s+-rf", "critical", "Recursive force delete"),
    (r"rm\s+-r", "high", "Recursive delete"),
    (r"del\s+/[fqs]", "critical", "Force delete files"),
    (r"rmdir\s+/", "critical", "Remove directory tree"),
    # Service operations
    (r"systemctl\s+stop", "high", "Stop systemd service"),
    (r"systemctl\s+restart", "high", "Restart systemd service"),
    (r"service\s+\w+\s+stop", "high", "Stop service"),
    (r"kill\s+-9", "high", "Force kill process"),
    (r"killall", "high", "Kill all processes"),
    # Permission changes
    (r"chmod\s+777", "high", "Grant full permissions"),
    (r"chmod\s+0", "critical", "Remove all permissions"),
    (r"chown\s+root", "high", "Change owner to root"),
    # Configuration changes
    (r">\s*/etc/", "critical", "Redirect to system file"),
    (r"echo\s+.*>\s*/etc/", "critical", "Write to system file"),
    (r"sed\s+-i.*\s+/etc/", "critical", "Modify system file in-place"),
    # Network operations
    (r"iptables\s+-F", "high", "Flush iptables rules"),
    (r"iptables\s+-X", "high", "Delete iptables chain"),
    (r"ufw\s+disable", "high", "Disable firewall"),
    # Database operations
    (r"DROP\s+TABLE", "critical", "Drop database table"),
    (r"DROP\s+DATABASE", "critical", "Drop entire database"),
    (r"TRUNCATE", "high", "Truncate table"),
    (r"DELETE\s+FROM.*WHERE", "medium", "Delete with condition"),
    (r"ALTER\s+TABLE.*DROP", "critical", "Drop table column"),
    # Container operations
    (r"docker\s+rm\s+-f", "high", "Force remove container"),
    (r"docker\s+rmi\s+-f", "high", "Force remove image"),
    (r"kubectl\s+delete", "high", "Delete K8s resource"),
    (r"kubectl\s+rollout\s+restart", "medium", "Restart K8s deployment"),
]


@dataclass
class CommandFeedback:
    """Feedback for a command execution result."""
    command: str
    risk_level: str
    risk_reason: str
    needs_approval: bool
    summary: str
    key_findings: List[str]
    warnings: List[str]
    errors: List[str]
    truncated: bool = False
    exit_code: Optional[int] = None


class OutputFormatter:
    """
    Formats command output for display and analysis.
    """

    def __init__(self, max_output_lines: int = 500, max_line_length: int = 200):
        self._max_lines = max_output_lines
        self._max_line_length = max_line_length

    def format_output(
        self,
        command: str,
        stdout: str,
        stderr: str,
        exit_code: Optional[int] = None,
    ) -> CommandFeedback:
        """Format command output into structured feedback."""
        risk_level, risk_reason = self._classify_risk(command)
        needs_approval = risk_level in (CommandRiskLevel.HIGH, CommandRiskLevel.CRITICAL)

        # Parse stdout
        findings, warnings, truncated = self._extract_findings(stdout)

        # Parse stderr for errors
        errors = self._extract_errors(stderr)

        # Build summary
        summary = self._build_summary(exit_code, stdout, stderr)

        return CommandFeedback(
            command=command,
            risk_level=risk_level,
            risk_reason=risk_reason,
            needs_approval=needs_approval,
            summary=summary,
            key_findings=findings,
            warnings=warnings,
            errors=errors,
            truncated=truncated,
            exit_code=exit_code,
        )

    def _classify_risk(self, command: str) -> tuple:
        """Classify command risk level."""
        for pattern, level, reason in DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return level, reason
        return CommandRiskLevel.LOW, "Read-only or low-risk operation"

    def _extract_findings(
        self, output: str
    ) -> tuple[List[str], List[str], bool]:
        """Extract key findings from command output."""
        findings = []
        warnings = []
        truncated = False

        lines = output.split("\n")
        if len(lines) > self._max_lines:
            truncated = True
            lines = lines[: self._max_lines]

        # Patterns that indicate important findings
        error_patterns = [
            r"error",
            r"fail(ed)?",
            r"exception",
            r"timeout",
            r"denied",
            r"refused",
            r"unavailable",
        ]
        warning_patterns = [
            r"warning",
            r"warn",
            r"deprecated",
        ]

        for line in lines:
            line_lower = line.lower()
            # Truncate long lines
            if len(line) > self._max_line_length:
                line = line[: self._max_line_length] + "..."

            for pattern in error_patterns:
                if re.search(pattern, line_lower):
                    findings.append(line.strip())
                    break
            else:
                for pattern in warning_patterns:
                    if re.search(pattern, line_lower):
                        warnings.append(line.strip())
                        break

        # Limit findings to avoid overwhelming output
        return findings[:20], warnings[:10], truncated

    def _extract_errors(self, stderr: str) -> List[str]:
        """Extract errors from stderr."""
        errors = []
        for line in stderr.split("\n"):
            if line.strip():
                if len(line) > self._max_line_length:
                    line = line[: self._max_line_length] + "..."
                errors.append(line.strip())
        return errors[:10]

    def _build_summary(
        self, exit_code: Optional[int], stdout: str, stderr: str
    ) -> str:
        """Build a human-readable summary of the execution."""
        lines = stdout.split("\n")
        output_lines = len([l for l in lines if l.strip()])

        status = "成功"
        if exit_code == 0:
            status = "成功"
        elif exit_code is None:
            status = "未知"
        else:
            status = f"失败 (exit={exit_code})"

        summary = f"执行{status}，输出 {output_lines} 行"
        if stderr:
            summary += f"，错误 {len(stderr.split(chr(10)))} 行"
        return summary


class ResultInterpreter:
    """
    Interprets command execution results to provide insights.
    """

    def interpret(
        self,
        command: str,
        output: str,
        exit_code: Optional[int],
    ) -> Dict[str, Any]:
        """
        Interpret command execution result.

        Returns structured interpretation with:
        - interpretation: Human-readable interpretation
        - findings: Key findings extracted
        - recommendations: Suggested next steps
        """
        interpretation = {
            "interpretation": "",
            "findings": [],
            "recommendations": [],
            "severity": "info",
        }

        # Empty output
        if not output.strip():
            if exit_code == 0:
                interpretation["interpretation"] = "命令执行成功，无输出"
                interpretation["findings"].append("命令正常完成")
            else:
                interpretation["interpretation"] = "命令执行失败，无错误输出"
                interpretation["severity"] = "warning"
            return interpretation

        # Count by type
        lines = output.split("\n")
        error_count = sum(
            1 for line in lines if re.search(r"error|fail|exception", line.lower())
        )
        warn_count = sum(
            1 for line in lines if re.search(r"warn(ing)?", line.lower())
        )
        success_count = sum(
            1 for line in lines if re.search(r"success|ok|created|ready", line.lower())
        )

        # Interpret based on counts
        if error_count > 0:
            interpretation["interpretation"] = f"检测到 {error_count} 个错误"
            interpretation["severity"] = "error"
            interpretation["findings"].append(f"包含 {error_count} 个错误信息")
        elif warn_count > 5:
            interpretation["interpretation"] = f"检测到 {warn_count} 个警告"
            interpretation["severity"] = "warning"
            interpretation["findings"].append(f"包含 {warn_count} 个警告信息")
        elif success_count > 0:
            interpretation["interpretation"] = "命令执行正常"
            interpretation["findings"].append(f"检测到 {success_count} 个成功标记")
        else:
            interpretation["interpretation"] = "命令执行完成"
            interpretation["findings"].append(f"共 {len(lines)} 行输出")

        return interpretation


# Global formatter and interpreter
_global_formatter: Optional[OutputFormatter] = None
_global_interpreter: Optional[ResultInterpreter] = None


def get_formatter() -> OutputFormatter:
    """Get the global formatter instance."""
    global _global_formatter
    if _global_formatter is None:
        _global_formatter = OutputFormatter()
    return _global_formatter


def get_interpreter() -> ResultInterpreter:
    """Get the global interpreter instance."""
    global _global_interpreter
    if _global_interpreter is None:
        _global_interpreter = ResultInterpreter()
    return _global_interpreter
