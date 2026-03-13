"""Git operations client for the task bus repository."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Optional

import git
import yaml

from models import TaskMeta, TaskStatus, WorkerState, generate_task_id

logger = logging.getLogger(__name__)


class GitConflictError(Exception):
    """Raised when a git push is rejected due to conflict."""


class WorkRepoClient:
    """Manages the target (work) repository where the agent performs its task.

    When a task has a ``repo_path``, the worker uses this class to:
    1. Create a dedicated branch ``share-work/<task_id>`` from the current HEAD.
    2. Run the AI agent with that branch checked out.
    3. Stage and commit all resulting changes on that branch.
    """

    BRANCH_PREFIX = "share-work"

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.repo = git.Repo(repo_path)

    def setup_branch(self, task_id: str) -> str:
        """Create and checkout a new branch for this task. Returns the branch name."""
        branch_name = f"{self.BRANCH_PREFIX}/{task_id}"
        if self.repo.is_dirty(untracked_files=True):
            logger.warning(
                "Work repository %s has uncommitted changes; branch will include them",
                self.repo_path,
            )
        self.repo.git.checkout("-b", branch_name)
        logger.info("Created work branch %s in %s", branch_name, self.repo_path)
        return branch_name

    def commit_results(self, branch_name: str, task_id: str, summary: str = "") -> bool:
        """Stage all changes and commit to *branch_name*.

        Returns True if there were changes to commit, False if the working tree
        was already clean.
        """
        self.repo.git.add("-A")
        # Check for staged changes
        if not self.repo.index.diff("HEAD") and not self.repo.untracked_files:
            logger.info("No changes to commit in work repo for task %s", task_id)
            return False
        first_line = summary.strip().splitlines()[0][:72] if summary.strip() else task_id
        commit_msg = f"feat({task_id}): {first_line}"
        self.repo.index.commit(commit_msg)
        logger.info("Committed work results to branch %s in %s", branch_name, self.repo_path)
        return True


class GitClient:
    """Manages git operations against the task-bus repository."""

    def __init__(self, repo_path: Path, remote: str = "origin", branch: str = "main"):
        self.repo_path = repo_path
        self.remote = remote
        self.branch = branch
        self.repo = git.Repo(repo_path)

    # ------------------------------------------------------------------
    # Low-level git helpers
    # ------------------------------------------------------------------

    def pull(self) -> None:
        """Pull latest changes from remote."""
        try:
            self.repo.remotes[self.remote].pull(self.branch)
            logger.debug("git pull succeeded")
        except git.GitCommandError as e:
            logger.warning("git pull failed: %s", e)
            raise

    def commit_and_push(self, message: str, paths: list[Path]) -> None:
        """Stage given paths, commit, and push. Raises GitConflictError on rejection."""
        for p in paths:
            rel = str(p.relative_to(self.repo_path))
            if p.exists():
                self.repo.index.add([rel])
            else:
                try:
                    self.repo.index.remove([rel], working_tree=True)
                except Exception:
                    pass

        if not self.repo.index.diff("HEAD") and not self.repo.untracked_files:
            logger.debug("Nothing to commit, skipping push")
            return

        self.repo.index.commit(message)
        try:
            self.repo.remotes[self.remote].push(self.branch)
            logger.debug("git push succeeded: %s", message)
        except git.GitCommandError as e:
            # Roll back the local commit so the caller can retry
            self.repo.git.reset("HEAD~1", "--soft")
            raise GitConflictError(f"push rejected: {e}") from e

    def commit_and_push_with_retry(
        self, message: str, paths: list[Path], max_retries: int = 4
    ) -> None:
        """Pull → commit → push with exponential back-off on conflict."""
        for attempt in range(max_retries + 1):
            try:
                self.pull()
                self.commit_and_push(message, paths)
                return
            except GitConflictError:
                if attempt == max_retries:
                    raise
                wait = 2 ** attempt
                logger.warning("Push conflict, retrying in %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)

    def delete_and_push(self, directory: Path, message: str) -> None:
        """Remove a directory, commit, and push."""
        if directory.exists():
            shutil.rmtree(directory)
        self.commit_and_push_with_retry(message, [directory])

    # ------------------------------------------------------------------
    # Task CRUD helpers
    # ------------------------------------------------------------------

    def task_dir(self, task_id: str) -> Path:
        return self.repo_path / "tasks" / task_id

    def meta_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "meta.yaml"

    def create_task(
        self,
        requirements: str,
        workplan: str,
        requested_by: str = "unknown",
        resources: Optional[dict] = None,
        priority: str = "normal",
        depends_on: Optional[list[str]] = None,
        repo_path: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> TaskMeta:
        """Create a new task directory and push to remote."""
        task_id = generate_task_id()
        task_path = self.task_dir(task_id)
        task_path.mkdir(parents=True, exist_ok=True)
        (task_path / "artifacts").mkdir(exist_ok=True)

        from models import Priority, ResourceRequirements, TaskMeta

        meta = TaskMeta(
            task_id=task_id,
            requested_by=requested_by,
            priority=Priority(priority),
            depends_on=depends_on or [],
            repo_path=repo_path,
            mode=mode,
        )
        if resources:
            meta.resources = ResourceRequirements.from_dict(resources)

        meta.save(self.meta_path(task_id))
        (task_path / "requirements.txt").write_text(requirements)
        (task_path / "workplan.md").write_text(workplan)
        (task_path / "artifacts" / ".gitkeep").touch()

        self.commit_and_push_with_retry(
            f"task: create {task_id}",
            [task_path],
        )
        logger.info("Created task %s", task_id)
        return meta

    def list_tasks(self, status: Optional[list[TaskStatus]] = None) -> list[TaskMeta]:
        """Return all tasks (optionally filtered by status) from local repo."""
        tasks_root = self.repo_path / "tasks"
        if not tasks_root.exists():
            return []
        result = []
        for meta_file in sorted(tasks_root.glob("*/meta.yaml")):
            try:
                meta = TaskMeta.load(meta_file)
                if status is None or meta.status in status:
                    result.append(meta)
            except Exception as e:
                logger.warning("Failed to read %s: %s", meta_file, e)
        return result

    def update_task_status(
        self,
        task_id: str,
        new_status: TaskStatus,
        worker_id: Optional[str] = None,
        extra_update: Optional[dict] = None,
    ) -> TaskMeta:
        """Atomically update task status with pull-commit-push."""
        self.pull()
        meta_file = self.meta_path(task_id)
        meta = TaskMeta.load(meta_file)
        meta.status = new_status
        if worker_id is not None:
            meta.assigned_to = worker_id
        if extra_update:
            for key, val in extra_update.items():
                # Support nested update for execution record
                if key == "execution" and isinstance(val, dict):
                    for ek, ev in val.items():
                        setattr(meta.execution, ek, ev)
                else:
                    setattr(meta, key, val)
        meta.save(meta_file)
        return meta

    def claim_task(self, task_id: str, worker_id: str) -> bool:
        """Try to claim a task. Returns True on success, False on conflict."""
        try:
            self.pull()
            meta_file = self.meta_path(task_id)
            if not meta_file.exists():
                return False
            meta = TaskMeta.load(meta_file)
            if meta.status != TaskStatus.OPEN:
                return False
            meta.status = TaskStatus.CLAIMED
            meta.assigned_to = worker_id
            meta.save(meta_file)
            self.commit_and_push(
                f"task: claim {task_id} by {worker_id}", [meta_file]
            )
            logger.info("Worker %s claimed task %s", worker_id, task_id)
            return True
        except GitConflictError:
            logger.debug("Claim conflict for %s, another worker got it", task_id)
            return False

    def start_task(self, task_id: str, worker_id: str, worker_node: str) -> None:
        """Transition task from claimed → in_progress."""
        from models import _now_iso

        self.update_task_status(
            task_id,
            TaskStatus.IN_PROGRESS,
            extra_update={
                "execution": {"started_at": _now_iso(), "worker_node": worker_node}
            },
        )
        meta_file = self.meta_path(task_id)
        self.commit_and_push_with_retry(
            f"task: start {task_id}", [meta_file]
        )

    def finish_task(self, task_id: str, success: bool) -> None:
        """Transition task to done or failed and push artifacts."""
        from models import TaskStatus, _now_iso

        new_status = TaskStatus.DONE if success else TaskStatus.FAILED
        self.pull()
        meta_file = self.meta_path(task_id)
        meta = TaskMeta.load(meta_file)
        meta.status = new_status
        meta.execution.finished_at = _now_iso()
        meta.save(meta_file)

        task_path = self.task_dir(task_id)
        artifact_paths = list((task_path / "artifacts").rglob("*"))
        self.commit_and_push_with_retry(
            f"task: {'done' if success else 'failed'} {task_id}",
            [meta_file] + artifact_paths,
        )
        logger.info("Task %s finished with status %s", task_id, new_status.value)

    def set_result_branch(self, task_id: str, branch: str) -> None:
        """Store the result branch name in task meta and push."""
        meta_file = self.meta_path(task_id)
        meta = TaskMeta.load(meta_file)
        meta.result_branch = branch
        meta.save(meta_file)
        self.commit_and_push_with_retry(
            f"task: set result_branch {task_id} -> {branch}", [meta_file]
        )
        logger.info("Recorded result_branch=%s for task %s", branch, task_id)

    def collect_and_cleanup(self, task_id: str, dest_dir: Path) -> None:
        """Copy artifacts to dest_dir and delete the task from the repo."""
        self.pull()
        task_path = self.task_dir(task_id)
        artifacts_src = task_path / "artifacts"
        if artifacts_src.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(artifacts_src, dest_dir / task_id, dirs_exist_ok=True)
            logger.info("Artifacts collected to %s/%s", dest_dir, task_id)
        self.delete_and_push(task_path, f"task: cleanup {task_id}")

    # ------------------------------------------------------------------
    # Worker status helpers
    # ------------------------------------------------------------------

    def worker_status_path(self, worker_id: str) -> Path:
        return self.repo_path / "workers" / worker_id / "status.yaml"

    def update_worker_status(self, state: WorkerState) -> None:
        path = self.worker_status_path(state.worker_id)
        state.save(path)
        try:
            self.commit_and_push_with_retry(
                f"worker: heartbeat {state.worker_id}", [path]
            )
        except Exception as e:
            logger.warning("Failed to push worker status: %s", e)

    def list_worker_states(self) -> list[WorkerState]:
        workers_root = self.repo_path / "workers"
        result = []
        for f in sorted(workers_root.glob("*/status.yaml")):
            try:
                result.append(WorkerState.load(f))
            except Exception as e:
                logger.warning("Failed to read worker status %s: %s", f, e)
        return result
