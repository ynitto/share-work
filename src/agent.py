"""AI Agent runner: invokes the CLI agent and captures artifacts."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class AgentRunner:
    """Wraps the Claude Code CLI (or any compatible agent binary)."""

    def __init__(
        self,
        binary: str = "claude",
        model: str = "claude-sonnet-4-6",
        timeout: int = 3600,
        sandbox: bool = True,
        max_tokens: int = 100000,
    ):
        self.binary = binary
        self.model = model
        self.timeout = timeout
        self.sandbox = sandbox
        self.max_tokens = max_tokens

    def run(
        self,
        requirements_path: Path,
        workplan_path: Path,
        output_dir: Path,
        extra_env: Optional[dict] = None,
    ) -> bool:
        """
        Run the AI agent for a task.

        Returns True on success, False on failure.
        The agent is expected to produce files inside output_dir.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        requirements = requirements_path.read_text(encoding="utf-8")
        workplan = workplan_path.read_text(encoding="utf-8") if workplan_path.exists() else ""

        prompt = self._build_prompt(requirements, workplan, output_dir)

        cmd = self._build_command(prompt, output_dir)
        env = {**os.environ, **(extra_env or {})}

        logger.info("Running agent: %s", " ".join(cmd[:3]) + " ...")
        try:
            result = subprocess.run(
                cmd,
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
    # Helpers
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

    def _build_command(self, prompt: str, output_dir: Path) -> list[str]:
        cmd = [
            self.binary,
            "--print",
            "--model", self.model,
            "--max-turns", "50",
        ]
        if not self.sandbox:
            cmd += ["--dangerously-skip-permissions"]
        cmd.append(prompt)
        return cmd

    @staticmethod
    def _write_error(output_dir: Path, message: str) -> None:
        (output_dir / "error.log").write_text(message, encoding="utf-8")
