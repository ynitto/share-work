"""AI Agent runner: invokes the CLI agent and captures artifacts.

Supported agent types:
- ``claude``       – Claude Code CLI (default)
- ``copilot``      – GitHub Copilot CLI  (``gh copilot suggest``)
- ``amazon-q``     – Amazon Q Developer CLI  (``q chat``)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class AgentRunner:
    """Base class for all agent CLI wrappers.

    Subclasses must implement :meth:`_build_command`.
    The default :meth:`_build_prompt` works for most agents; override when
    the target CLI expects a different prompt style.
    """

    def __init__(
        self,
        binary: str,
        model: str = "",
        timeout: int = 3600,
        sandbox: bool = True,
        max_tokens: int = 100000,
    ):
        self.binary = binary
        self.model = model
        self.timeout = timeout
        self.sandbox = sandbox
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        requirements_path: Path,
        workplan_path: Path,
        output_dir: Path,
        extra_env: Optional[dict] = None,
    ) -> bool:
        """Run the AI agent for a task.

        Returns True on success, False on failure.
        The agent is expected to produce files inside *output_dir*.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        requirements = requirements_path.read_text(encoding="utf-8")
        workplan = workplan_path.read_text(encoding="utf-8") if workplan_path.exists() else ""

        prompt = self._build_prompt(requirements, workplan, output_dir)

        cmd, stdin_data = self._build_command(prompt, output_dir)
        env = {**os.environ, **(extra_env or {})}

        logger.info("Running agent: %s", " ".join(cmd[:3]) + " ...")
        try:
            result = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
                cwd=str(output_dir),
            )
        except subprocess.TimeoutExpired:
            logger.error("Agent timed out after %ds", self.timeout)
            self._write_error(output_dir, f"Agent timed out after {self.timeout} seconds")
            return False
        except FileNotFoundError:
            logger.error("Agent binary not found: %s", self.binary)
            self._write_error(output_dir, f"Agent binary not found: {self.binary}")
            return False

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        if stdout:
            (output_dir / "agent_stdout.txt").write_text(stdout, encoding="utf-8")

        if result.returncode != 0:
            logger.error("Agent failed (exit %d): %s", result.returncode, stderr[:500])
            self._write_error(output_dir, f"Exit code {result.returncode}\n\n{stderr}")
            return False

        # Ensure result.md exists as the canonical output
        result_md = output_dir / "result.md"
        if not result_md.exists():
            result_md.write_text(
                f"# Task Result\n\n{stdout}\n",
                encoding="utf-8",
            )

        logger.info("Agent completed successfully, output in %s", output_dir)
        return True

    # ------------------------------------------------------------------
    # Overridable helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, requirements: str, workplan: str, output_dir: Path) -> str:
        plan_section = f"\n\n## Work Plan\n{workplan}" if workplan.strip() else ""
        return (
            f"# Task Requirements\n\n{requirements}"
            f"{plan_section}\n\n"
            f"## Output Instructions\n"
            f"Write all results to the current directory (`{output_dir}`).\n"
            f"Create a file named `result.md` summarising what you did and the outcomes.\n"
            f"Include any generated code or documents as separate files.\n"
        )

    def _build_command(self, prompt: str, output_dir: Path) -> tuple[list[str], Optional[str]]:
        """Return ``(argv, stdin_data)``.

        *stdin_data* is ``None`` when the prompt is passed as a CLI argument,
        or a string when it should be written to the process's stdin.
        """
        raise NotImplementedError

    @staticmethod
    def _write_error(output_dir: Path, message: str) -> None:
        (output_dir / "error.log").write_text(message, encoding="utf-8")


# ---------------------------------------------------------------------------
# Claude Code CLI
# ---------------------------------------------------------------------------

class ClaudeAgentRunner(AgentRunner):
    """Wraps the Claude Code CLI (``claude --print``).

    Requires Claude Code to be installed:
      https://github.com/anthropics/claude-code
    """

    def __init__(
        self,
        binary: str = "claude",
        model: str = "claude-sonnet-4-6",
        timeout: int = 3600,
        sandbox: bool = True,
        max_tokens: int = 100000,
    ):
        super().__init__(
            binary=binary,
            model=model,
            timeout=timeout,
            sandbox=sandbox,
            max_tokens=max_tokens,
        )

    def _build_command(self, prompt: str, output_dir: Path) -> tuple[list[str], Optional[str]]:
        cmd = [
            self.binary,
            "--print",
            "--model", self.model,
            "--max-turns", "50",
        ]
        if not self.sandbox:
            cmd += ["--dangerously-skip-permissions"]
        cmd.append(prompt)
        return cmd, None


# ---------------------------------------------------------------------------
# GitHub Copilot CLI
# ---------------------------------------------------------------------------

class CopilotAgentRunner(AgentRunner):
    """Wraps the GitHub Copilot CLI extension (``gh copilot suggest``).

    Requires the GitHub CLI with the Copilot extension installed::

        gh extension install github/gh-copilot

    The Copilot CLI specialises in shell-command suggestions.  The task
    requirement is condensed into a single-line prompt and the suggested
    command is captured as the result.

    Configuration keys (under ``agent:``):
        binary  – path to ``gh`` binary (default: ``"gh"``)
        type    – ``"shell"`` | ``"git"`` | ``"gh"`` (default: ``"shell"``)
    """

    def __init__(
        self,
        binary: str = "gh",
        timeout: int = 3600,
        suggestion_type: str = "shell",
        **kwargs,
    ):
        super().__init__(binary=binary, timeout=timeout, **kwargs)
        self.suggestion_type = suggestion_type

    def _build_prompt(self, requirements: str, workplan: str, output_dir: Path) -> str:
        # Copilot suggest works best with a concise, single-line description
        first_line = requirements.strip().splitlines()[0]
        plan_hint = f" ({workplan.strip().splitlines()[0]})" if workplan.strip() else ""
        return f"{first_line}{plan_hint}"

    def _build_command(self, prompt: str, output_dir: Path) -> tuple[list[str], Optional[str]]:
        return (
            [self.binary, "copilot", "suggest", "-t", self.suggestion_type, prompt],
            None,
        )


# ---------------------------------------------------------------------------
# Amazon Q Developer CLI
# ---------------------------------------------------------------------------

class AmazonQAgentRunner(AgentRunner):
    """Wraps the Amazon Q Developer CLI (``q chat``).

    Requires Amazon Q CLI to be installed and authenticated::

        https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-installing.html

    The prompt is fed via stdin so that ``q chat`` operates in
    non-interactive (pipe) mode.

    Configuration keys (under ``agent:``):
        binary  – path to ``q`` binary (default: ``"q"``)
    """

    def __init__(
        self,
        binary: str = "q",
        timeout: int = 3600,
        **kwargs,
    ):
        super().__init__(binary=binary, timeout=timeout, **kwargs)

    def _build_command(self, prompt: str, output_dir: Path) -> tuple[list[str], Optional[str]]:
        # Amazon Q reads from stdin when not attached to a terminal
        return [self.binary, "chat"], prompt


# ---------------------------------------------------------------------------
# Registry & factory
# ---------------------------------------------------------------------------

#: Maps agent type names (and aliases) to their runner classes.
AGENT_REGISTRY: dict[str, type[AgentRunner]] = {
    "claude": ClaudeAgentRunner,
    "copilot": CopilotAgentRunner,
    "github-copilot": CopilotAgentRunner,
    "gh-copilot": CopilotAgentRunner,
    "amazon-q": AmazonQAgentRunner,
    "amazonq": AmazonQAgentRunner,
    "q": AmazonQAgentRunner,
}

#: Default binary name for each runner class.
_DEFAULT_BINARY: dict[type[AgentRunner], str] = {
    ClaudeAgentRunner: "claude",
    CopilotAgentRunner: "gh",
    AmazonQAgentRunner: "q",
}


def create_agent_runner(
    agent_type: str = "claude",
    binary: Optional[str] = None,
    model: str = "claude-sonnet-4-6",
    timeout: int = 3600,
    sandbox: bool = True,
    suggestion_type: str = "shell",
) -> AgentRunner:
    """Factory: create the appropriate :class:`AgentRunner` for *agent_type*.

    Args:
        agent_type:      One of ``"claude"``, ``"copilot"``/``"github-copilot"``,
                         ``"amazon-q"``/``"q"``.
        binary:          Override the default CLI binary path.
        model:           Model name (Claude only).
        timeout:         Subprocess timeout in seconds.
        sandbox:         Enable sandbox mode (Claude only).
        suggestion_type: Copilot suggestion type: ``"shell"``, ``"git"``, or ``"gh"``.

    Raises:
        ValueError: If *agent_type* is not recognised.
    """
    runner_cls = AGENT_REGISTRY.get(agent_type.lower())
    if runner_cls is None:
        available = ", ".join(sorted(set(AGENT_REGISTRY)))
        raise ValueError(
            f"Unknown agent type '{agent_type}'. Available: {available}"
        )

    effective_binary = binary or _DEFAULT_BINARY[runner_cls]

    if runner_cls is ClaudeAgentRunner:
        return ClaudeAgentRunner(
            binary=effective_binary,
            model=model,
            timeout=timeout,
            sandbox=sandbox,
        )
    if runner_cls is CopilotAgentRunner:
        return CopilotAgentRunner(
            binary=effective_binary,
            timeout=timeout,
            suggestion_type=suggestion_type,
        )
    if runner_cls is AmazonQAgentRunner:
        return AmazonQAgentRunner(
            binary=effective_binary,
            timeout=timeout,
        )

    # Fallback for any future subclass that doesn't need special kwargs
    return runner_cls(binary=effective_binary, timeout=timeout)  # type: ignore[call-arg]
