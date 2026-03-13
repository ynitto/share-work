"""Worker: polls GitLab, claims tasks, and runs the AI agent."""

from __future__ import annotations

import logging
import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import psutil
import yaml

from agent import AgentRunner, create_agent_runner
from git_client import GitClient, GitConflictError, WorkRepoClient
from models import Priority, TaskMeta, TaskStatus, WorkerResources, WorkerState, WorkerStatus, PRIORITY_ORDER, _now_iso, seconds_since

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "worker_id": f"worker-{socket.gethostname()}",
    "gitlab": {
        "repo_path": ".",
        "remote": "origin",
        "branch": "main",
    },
    "polling": {
        "worker_interval": 30,
        "heartbeat_interval": 60,
    },
    "execution": {
        "max_concurrent_tasks": 3,
        "agent_type": "claude",
        "agent_binary": None,
        "agent_timeout": 3600,
        "agent_model": "claude-sonnet-4-6",
        "agent_sandbox": True,
        "agent_suggestion_type": "shell",
        "self_order_delay": 0,   # seconds to wait before claiming own tasks (0 = disabled)
        "owner_ids": [],         # requested_by values treated as "self" (worker_id always included)
    },
    "capabilities": [],
    "resources": {
        "cpu_total": os.cpu_count() or 4,
        "memory_total": psutil.virtual_memory().total // (1024 * 1024),
        "disk_total": psutil.disk_usage("/").total // (1024 * 1024),
        "has_gpu": False,
    },
    "health": {
        "enabled": True,
        "port": 8765,
    },
}


class Worker:
    """Main worker daemon."""

    def __init__(self, config: dict):
        self.config = self._merge(DEFAULT_CONFIG, config)
        self.worker_id: str = self.config["worker_id"]
        self.worker_node: str = platform.node()

        repo_path = Path(self.config["gitlab"]["repo_path"]).resolve()
        self.git = GitClient(
            repo_path=repo_path,
            remote=self.config["gitlab"]["remote"],
            branch=self.config["gitlab"]["branch"],
        )

        exec_cfg = self.config["execution"]
        self.agent = create_agent_runner(
            agent_type=exec_cfg.get("agent_type", "claude"),
            binary=exec_cfg.get("agent_binary") or None,
            model=exec_cfg.get("agent_model", "claude-sonnet-4-6"),
            timeout=exec_cfg.get("agent_timeout", 3600),
            sandbox=exec_cfg.get("agent_sandbox", True),
            suggestion_type=exec_cfg.get("agent_suggestion_type", "shell"),
        )

        self.max_concurrent: int = exec_cfg.get("max_concurrent_tasks", 3)
        self.self_order_delay: int = exec_cfg.get("self_order_delay", 0)
        configured_owners: list[str] = list(exec_cfg.get("owner_ids", []))
        if self.worker_id not in configured_owners:
            configured_owners.append(self.worker_id)
        self.owner_ids: list[str] = configured_owners
        self.capabilities: list[str] = self.config.get("capabilities", [])
        self._semaphore = threading.Semaphore(self.max_concurrent)
        self._active_tasks: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._running = False

        res_cfg = self.config.get("resources", {})
        self._resources = WorkerResources(
            cpu_total=res_cfg.get("cpu_total", os.cpu_count() or 4),
            memory_total=res_cfg.get("memory_total", psutil.virtual_memory().total // (1024 * 1024)),
            disk_total=res_cfg.get("disk_total", psutil.disk_usage("/").total // (1024 * 1024)),
            has_gpu=res_cfg.get("has_gpu", False),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main daemon loop."""
        poll_interval = self.config["polling"]["worker_interval"]
        heartbeat_interval = self.config["polling"]["heartbeat_interval"]

        self._running = True
        logger.info("Worker %s started (interval=%ds)", self.worker_id, poll_interval)

        last_heartbeat = 0.0

        while self._running:
            now = time.monotonic()

            # Heartbeat
            if now - last_heartbeat >= heartbeat_interval:
                self._update_heartbeat()
                last_heartbeat = now

            # Task polling
            try:
                self.git.pull()
            except Exception as e:
                logger.warning("Pull failed: %s", e)
                time.sleep(poll_interval)
                continue

            if self._semaphore._value > 0:  # noqa: SLF001
                self._try_claim_one()

            time.sleep(poll_interval)

        logger.info("Worker %s stopped", self.worker_id)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Claim & execute
    # ------------------------------------------------------------------

    def _try_claim_one(self) -> None:
        open_tasks = self.git.list_tasks(status=[TaskStatus.OPEN])
        # Sort by priority then creation time
        open_tasks.sort(
            key=lambda t: (PRIORITY_ORDER.get(t.priority, 99), t.created_at)
        )
        for meta in open_tasks:
            if not self._can_handle(meta):
                continue
            if self.git.claim_task(meta.task_id, self.worker_id):
                self._launch(meta.task_id)
                return

    def _launch(self, task_id: str) -> None:
        """Launch task execution in a background thread."""
        if not self._semaphore.acquire(blocking=False):
            logger.warning("No free slot for %s, skipping", task_id)
            return
        thread = threading.Thread(
            target=self._execute,
            args=(task_id,),
            daemon=True,
            name=f"task-{task_id}",
        )
        with self._lock:
            self._active_tasks[task_id] = thread
        thread.start()

    def _execute(self, task_id: str) -> None:
        try:
            self._run_task(task_id)
        finally:
            self._semaphore.release()
            with self._lock:
                self._active_tasks.pop(task_id, None)

    def _run_task(self, task_id: str) -> None:
        task_dir = self.git.task_dir(task_id)
        artifacts_dir = task_dir / "artifacts"
        requirements_path = task_dir / "requirements.txt"
        workplan_path = task_dir / "workplan.md"

        # Load meta to get repo_path before starting
        from models import TaskMeta
        meta = TaskMeta.load(self.git.meta_path(task_id))

        # Transition to in_progress
        try:
            self.git.start_task(task_id, self.worker_id, self.worker_node)
        except Exception as e:
            logger.error("Failed to start task %s: %s", task_id, e)
            return

        # Set up work repository branch if repo_path is specified
        work_dir: Optional[Path] = None
        result_branch: Optional[str] = None
        work_repo: Optional[WorkRepoClient] = None

        if meta.repo_path:
            repo_path = Path(meta.repo_path)
            if not repo_path.exists():
                logger.error(
                    "Task %s: repo_path does not exist: %s", task_id, repo_path
                )
                try:
                    self.git.finish_task(task_id, success=False)
                except Exception:
                    pass
                return
            try:
                work_repo = WorkRepoClient(repo_path)
                result_branch = work_repo.setup_branch(task_id)
                work_dir = repo_path
                # Persist result_branch in meta so observers can see it
                self.git.set_result_branch(task_id, result_branch)
            except Exception as e:
                logger.error(
                    "Failed to set up work branch for task %s: %s", task_id, e
                )
                try:
                    self.git.finish_task(task_id, success=False)
                except Exception:
                    pass
                return

        logger.info(
            "Executing task %s (work_dir=%s)",
            task_id,
            work_dir or artifacts_dir,
        )
        success = self.agent.run(
            requirements_path=requirements_path,
            workplan_path=workplan_path,
            output_dir=artifacts_dir,
            work_dir=work_dir,
        )

        # Commit agent results to the target repository branch
        if work_repo and result_branch:
            try:
                requirements_text = requirements_path.read_text(encoding="utf-8")
                work_repo.commit_results(result_branch, task_id, summary=requirements_text)
            except Exception as e:
                logger.error(
                    "Failed to commit work results for task %s: %s", task_id, e
                )
                success = False

        try:
            self.git.finish_task(task_id, success)
        except Exception as e:
            logger.error("Failed to finish task %s: %s", task_id, e)

    # ------------------------------------------------------------------
    # Resource checks
    # ------------------------------------------------------------------

    def _can_handle(self, meta: TaskMeta) -> bool:
        req = meta.resources
        avail = self._get_available_resources()
        skills_ok = all(s in self.capabilities for s in req.required_skills)
        gpu_ok = (not req.gpu) or self._resources.has_gpu
        if not (
            avail["cpu"] >= req.cpu
            and avail["memory"] >= req.memory
            and avail["disk"] >= req.disk
            and skills_ok
            and gpu_ok
        ):
            return False

        # Self-order delay: wait before claiming tasks submitted by this worker
        if self.self_order_delay > 0 and meta.requested_by in self.owner_ids:
            age = seconds_since(meta.created_at)
            if age < self.self_order_delay:
                logger.debug(
                    "Self-order delay: skipping %s (age=%.0fs < delay=%ds, requested_by=%s)",
                    meta.task_id, age, self.self_order_delay, meta.requested_by,
                )
                return False

        return True

    def _get_available_resources(self) -> dict:
        cpu_used_pct = psutil.cpu_percent(interval=0.1)
        cpu_available = max(0, int(self._resources.cpu_total * (1 - cpu_used_pct / 100)))
        mem = psutil.virtual_memory()
        mem_available = mem.available // (1024 * 1024)
        disk = psutil.disk_usage("/")
        disk_available = disk.free // (1024 * 1024)
        return {"cpu": cpu_available, "memory": mem_available, "disk": disk_available}

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _update_heartbeat(self) -> None:
        avail = self._get_available_resources()
        state = WorkerState(
            worker_id=self.worker_id,
            status=WorkerStatus.BUSY if self._active_tasks else WorkerStatus.IDLE,
            capabilities=self.capabilities,
            current_tasks=list(self._active_tasks.keys()),
            resources=WorkerResources(
                cpu_total=self._resources.cpu_total,
                cpu_available=avail["cpu"],
                memory_total=self._resources.memory_total,
                memory_available=avail["memory"],
                disk_total=self._resources.disk_total,
                disk_available=avail["disk"],
                has_gpu=self._resources.has_gpu,
            ),
        )
        try:
            self.git.update_worker_status(state)
            logger.debug("Heartbeat updated for %s", self.worker_id)
        except Exception as e:
            logger.warning("Heartbeat push failed: %s", e)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _merge(base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(result.get(k), dict):
                result[k] = Worker._merge(result[k], v)
            else:
                result[k] = v
        return result


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

    parser = argparse.ArgumentParser(description="Distributed AI Task Worker")
    parser.add_argument("--config", "-c", default="config/worker.yaml")
    parser.add_argument("--worker-id", help="Override worker_id from config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.worker_id:
        cfg["worker_id"] = args.worker_id

    worker = Worker(cfg)
    try:
        worker.run()
    except KeyboardInterrupt:
        logger.info("Worker interrupted")
        worker.stop()


if __name__ == "__main__":
    main()
