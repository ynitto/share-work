"""Controller: task decomposition, monitoring, artifact collection."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from git_client import GitClient
from models import Priority, TaskMeta, TaskStatus, _now_iso, seconds_since

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "gitlab": {
        "repo_path": ".",
        "remote": "origin",
        "branch": "main",
    },
    "polling": {
        "controller_interval": 60,
        "decompose_model": "claude-sonnet-4-6",
        "decompose_binary": "claude",
    },
    "timeouts": {
        "claim_ttl": 300,
        "execution_ttl": 3600,
    },
    "cleanup": {
        "enabled": True,
        "keep_failed_tasks": True,
        "artifacts_dir": "./collected_artifacts",
    },
}



class TaskDecomposer:
    """Uses an AI model to decompose a natural-language requirement into tasks."""

    def __init__(self, model: str = "claude-sonnet-4-6", binary: str = "claude"):
        self.model = model
        self.binary = binary

    def decompose(self, requirement: str, requested_by: str = "unknown") -> list[dict]:
        """
        Return a list of task specs, each as:
          {requirements, workplan, priority, resources, depends_on}

        Falls back to a single task if the AI call is not available.
        """
        try:
            return self._ai_decompose(requirement, requested_by)
        except Exception as e:
            logger.warning("AI decompose failed (%s), using single-task fallback", e)
            return self._fallback_decompose(requirement)

    def _ai_decompose(self, requirement: str, requested_by: str) -> list[dict]:
        prompt = f"""You are a task planner for a distributed AI agent system.
Break down the following requirement into one or more concrete tasks.
Return a JSON array. Each element must have:
  - requirements (string): clear natural-language description of what to do
  - workplan (string): step-by-step markdown plan for an AI agent to follow
  - priority (string): one of low/normal/high/critical
  - resources (object): {{cpu: int, memory: int (MB), disk: int (MB), gpu: bool, required_skills: [str]}}
  - depends_on (array of int): indices of tasks this one depends on (0-based, use [] for none)

Requirement: {requirement}

Respond with ONLY the JSON array, no other text."""

        result = subprocess.run(
            [self.binary, "--print", "--model", self.model, prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"{self.binary} CLI error: {result.stderr}")

        raw = result.stdout.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            raw = raw.rstrip("`").rstrip()

        specs = json.loads(raw)
        return specs

    def _fallback_decompose(self, requirement: str) -> list[dict]:
        return [
            {
                "requirements": requirement,
                "workplan": f"# Task Plan\n\n## Objective\n{requirement}\n\n## Steps\n1. Analyze the requirement\n2. Implement the solution\n3. Verify the output\n",
                "priority": "normal",
                "resources": {"cpu": 1, "memory": 1024, "disk": 512, "gpu": False, "required_skills": []},
                "depends_on": [],
            }
        ]


class Controller:
    """Main controller daemon."""

    def __init__(self, config: dict):
        self.config = {**DEFAULT_CONFIG, **config}
        repo_path = Path(self.config["gitlab"]["repo_path"]).resolve()
        self.git = GitClient(
            repo_path=repo_path,
            remote=self.config["gitlab"]["remote"],
            branch=self.config["gitlab"]["branch"],
        )
        self.decomposer = TaskDecomposer(
            model=self.config["polling"].get("decompose_model", "claude-sonnet-4-6"),
            binary=self.config["polling"].get("decompose_binary", "claude"),
        )
        self.artifacts_dir = Path(self.config["cleanup"].get("artifacts_dir", "./collected_artifacts"))
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        requirement: str,
        requested_by: str = "unknown",
        repo_path: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> list[str]:
        """Decompose requirement and push tasks to the task bus. Returns list of task_ids.

        Args:
            repo_path: Optional path to a local Git repository where the worker
                       will create a branch and commit results.
            mode:      ``"local"`` to execute immediately on this machine;
                       ``None`` (default) for normal worker-polling mode.
        """
        logger.info("Decomposing requirement from %s (mode=%s)", requested_by, mode or "normal")
        if repo_path:
            logger.info("  Work repo: %s", repo_path)
        specs = self.decomposer.decompose(requirement, requested_by)

        # Resolve depends_on indices → task_ids created so far
        created_ids: list[str] = []
        for spec in specs:
            raw_depends = spec.pop("depends_on", [])
            depends_on = [created_ids[i] for i in raw_depends if i < len(created_ids)]
            meta = self.git.create_task(
                requirements=spec["requirements"],
                workplan=spec.get("workplan", ""),
                requested_by=requested_by,
                resources=spec.get("resources"),
                priority=spec.get("priority", "normal"),
                depends_on=depends_on,
                repo_path=repo_path,
                mode=mode,
            )
            created_ids.append(meta.task_id)
            logger.info("  Submitted task %s", meta.task_id)

        return created_ids

    def run_once(self) -> None:
        """Single iteration: pull, handle done/failed/timeout."""
        try:
            self.git.pull()
        except Exception as e:
            logger.warning("Pull failed: %s", e)
            return

        tasks = self.git.list_tasks()
        for meta in tasks:
            try:
                self._handle_task(meta)
            except Exception as e:
                logger.error("Error handling task %s: %s", meta.task_id, e)

    def run(self) -> None:
        """Main daemon loop."""
        interval = self.config["polling"]["controller_interval"]
        self._running = True
        logger.info("Controller started (interval=%ds)", interval)
        while self._running:
            self.run_once()
            time.sleep(interval)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _handle_task(self, meta: TaskMeta) -> None:
        if meta.status == TaskStatus.DONE:
            self._collect(meta)
        elif meta.status == TaskStatus.FAILED:
            self._handle_failed(meta)
        elif meta.status == TaskStatus.CLAIMED:
            self._check_claim_timeout(meta)
        elif meta.status == TaskStatus.IN_PROGRESS:
            self._check_execution_timeout(meta)
        elif meta.status == TaskStatus.OPEN:
            self._check_blocked(meta)

    def _collect(self, meta: TaskMeta) -> None:
        logger.info("Collecting artifacts for task %s", meta.task_id)
        self.git.collect_and_cleanup(meta.task_id, self.artifacts_dir)
        self._unblock_dependents(meta.task_id)

    def _handle_failed(self, meta: TaskMeta) -> None:
        if meta.execution.retry_count < meta.execution.max_retries:
            logger.info(
                "Retrying failed task %s (attempt %d/%d)",
                meta.task_id,
                meta.execution.retry_count + 1,
                meta.execution.max_retries,
            )
            meta.status = TaskStatus.OPEN
            meta.assigned_to = None
            meta.execution.retry_count += 1
            meta.execution.started_at = None
            meta.execution.finished_at = None
            meta.save(self.git.meta_path(meta.task_id))
            self.git.commit_and_push_with_retry(
                f"task: reopen {meta.task_id} for retry",
                [self.git.meta_path(meta.task_id)],
            )
        else:
            logger.warning("Task %s exceeded max retries, keeping as failed", meta.task_id)
            if not self.config["cleanup"].get("keep_failed_tasks", True):
                self.git.collect_and_cleanup(meta.task_id, self.artifacts_dir)

    def _check_claim_timeout(self, meta: TaskMeta) -> None:
        ttl = meta.timeouts.claim_ttl
        elapsed = seconds_since(meta.updated_at)
        if elapsed > ttl:
            logger.warning(
                "Task %s stuck in 'claimed' for %.0fs (ttl=%ds), reopening",
                meta.task_id, elapsed, ttl,
            )
            meta.status = TaskStatus.OPEN
            meta.assigned_to = None
            meta.save(self.git.meta_path(meta.task_id))
            self.git.commit_and_push_with_retry(
                f"task: timeout-reopen {meta.task_id} (claim ttl)",
                [self.git.meta_path(meta.task_id)],
            )

    def _check_execution_timeout(self, meta: TaskMeta) -> None:
        ttl = meta.timeouts.execution_ttl
        elapsed = seconds_since(meta.execution.started_at)
        if elapsed > ttl:
            logger.warning(
                "Task %s timed out in execution (%.0fs > %ds)",
                meta.task_id, elapsed, ttl,
            )
            if meta.execution.retry_count < meta.execution.max_retries:
                meta.status = TaskStatus.OPEN
                meta.assigned_to = None
                meta.execution.retry_count += 1
                meta.execution.started_at = None
            else:
                meta.status = TaskStatus.FAILED
            meta.save(self.git.meta_path(meta.task_id))
            self.git.commit_and_push_with_retry(
                f"task: timeout {'reopen' if meta.status == TaskStatus.OPEN else 'fail'} {meta.task_id}",
                [self.git.meta_path(meta.task_id)],
            )

    def _check_blocked(self, meta: TaskMeta) -> None:
        """Keep task in open unless all dependencies are done (already cleaned up)."""
        # Dependencies that are done get deleted, so if the task_dir is gone it's done.
        for dep_id in meta.depends_on:
            if (self.git.task_dir(dep_id)).exists():
                logger.debug("Task %s blocked by %s", meta.task_id, dep_id)
                return  # Still waiting

    def _unblock_dependents(self, completed_task_id: str) -> None:
        """After a task completes, check if any open tasks had it as a dependency."""
        open_tasks = self.git.list_tasks(status=[TaskStatus.OPEN])
        for meta in open_tasks:
            if completed_task_id in meta.depends_on:
                logger.info(
                    "Task %s dependency %s resolved", meta.task_id, completed_task_id
                )


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def load_config(path: Optional[str]) -> dict:
    if path and Path(path).exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Distributed AI Task Controller")
    sub = parser.add_subparsers(dest="cmd")

    # Run daemon
    run_p = sub.add_parser("run", help="Start the controller daemon")
    run_p.add_argument("--config", "-c", default="config/controller.yaml")

    # Submit a task manually
    submit_p = sub.add_parser("submit", help="Submit a task requirement")
    submit_p.add_argument("--config", "-c", default="config/controller.yaml")
    submit_p.add_argument("--requirement", "-r", required=True, help="Natural language requirement")
    submit_p.add_argument("--by", default="cli-user", help="Requester name")

    # List tasks
    list_p = sub.add_parser("list", help="List tasks")
    list_p.add_argument("--config", "-c", default="config/controller.yaml")
    list_p.add_argument("--status", nargs="*", help="Filter by status")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    cfg = load_config(args.config)
    controller = Controller(cfg)

    if args.cmd == "run":
        try:
            controller.run()
        except KeyboardInterrupt:
            logger.info("Controller stopped")

    elif args.cmd == "submit":
        task_ids = controller.submit(args.requirement, requested_by=args.by)
        print("Submitted tasks:")
        for tid in task_ids:
            print(f"  {tid}")

    elif args.cmd == "list":
        controller.git.pull()
        status_filter = None
        if args.status:
            status_filter = [TaskStatus(s) for s in args.status]
        tasks = controller.git.list_tasks(status=status_filter)
        if not tasks:
            print("No tasks found.")
        for meta in tasks:
            print(
                f"  [{meta.status.value:12s}] {meta.task_id}  priority={meta.priority.value}"
                f"  assigned={meta.assigned_to or '-'}"
            )


if __name__ == "__main__":
    main()
