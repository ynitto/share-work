"""Data models for the distributed AI agent task system."""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml


class TaskStatus(str, Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


PRIORITY_ORDER = {
    Priority.CRITICAL: 0,
    Priority.HIGH: 1,
    Priority.NORMAL: 2,
    Priority.LOW: 3,
}


class WorkerStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def seconds_since(iso_str: Optional[str]) -> float:
    """Return seconds elapsed since the given ISO-8601 timestamp. Returns 0 on parse error."""
    if not iso_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 0.0


def generate_task_id() -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"task-{date_str}-{suffix}"


@dataclass
class ResourceRequirements:
    cpu: int = 1
    memory: int = 1024       # MB
    disk: int = 512           # MB
    gpu: bool = False
    required_skills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cpu": self.cpu,
            "memory": self.memory,
            "disk": self.disk,
            "gpu": self.gpu,
            "required_skills": self.required_skills,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ResourceRequirements":
        return cls(
            cpu=d.get("cpu", 1),
            memory=d.get("memory", 1024),
            disk=d.get("disk", 512),
            gpu=d.get("gpu", False),
            required_skills=d.get("required_skills", []),
        )


@dataclass
class ExecutionRecord:
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    worker_node: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "worker_node": self.worker_node,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExecutionRecord":
        return cls(
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            worker_node=d.get("worker_node"),
            retry_count=d.get("retry_count", 0),
            max_retries=d.get("max_retries", 3),
        )


@dataclass
class Timeouts:
    claim_ttl: int = 300       # seconds
    execution_ttl: int = 3600  # seconds

    def to_dict(self) -> dict:
        return {"claim_ttl": self.claim_ttl, "execution_ttl": self.execution_ttl}

    @classmethod
    def from_dict(cls, d: dict) -> "Timeouts":
        return cls(
            claim_ttl=d.get("claim_ttl", 300),
            execution_ttl=d.get("execution_ttl", 3600),
        )


@dataclass
class TaskMeta:
    task_id: str
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    status: TaskStatus = TaskStatus.OPEN
    requested_by: str = "unknown"
    assigned_to: Optional[str] = None
    priority: Priority = Priority.NORMAL
    deadline: Optional[str] = None
    resources: ResourceRequirements = field(default_factory=ResourceRequirements)
    depends_on: list[str] = field(default_factory=list)
    timeouts: Timeouts = field(default_factory=Timeouts)
    execution: ExecutionRecord = field(default_factory=ExecutionRecord)
    # Target repository for work output (optional)
    repo_path: Optional[str] = None
    result_branch: Optional[str] = None
    # Execution mode: None (normal worker polling) | "local" (immediate local execution)
    mode: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status.value,
            "requested_by": self.requested_by,
            "assigned_to": self.assigned_to,
            "priority": self.priority.value,
            "deadline": self.deadline,
            "resources": self.resources.to_dict(),
            "depends_on": self.depends_on,
            "timeouts": self.timeouts.to_dict(),
            "execution": self.execution.to_dict(),
            "repo_path": self.repo_path,
            "result_branch": self.result_branch,
            "mode": self.mode,
        }

    def save(self, path: Path) -> None:
        self.updated_at = _now_iso()
        path.write_text(yaml.dump(self.to_dict(), allow_unicode=True, sort_keys=False))

    @classmethod
    def from_dict(cls, d: dict) -> "TaskMeta":
        return cls(
            task_id=d["task_id"],
            created_at=d.get("created_at", _now_iso()),
            updated_at=d.get("updated_at", _now_iso()),
            status=TaskStatus(d.get("status", "open")),
            requested_by=d.get("requested_by", "unknown"),
            assigned_to=d.get("assigned_to"),
            priority=Priority(d.get("priority", "normal")),
            deadline=d.get("deadline"),
            resources=ResourceRequirements.from_dict(d.get("resources", {})),
            depends_on=d.get("depends_on", []),
            timeouts=Timeouts.from_dict(d.get("timeouts", {})),
            execution=ExecutionRecord.from_dict(d.get("execution", {})),
            repo_path=d.get("repo_path"),
            result_branch=d.get("result_branch"),
            mode=d.get("mode"),
        )

    @classmethod
    def load(cls, path: Path) -> "TaskMeta":
        return cls.from_dict(yaml.safe_load(path.read_text()))


@dataclass
class WorkerResources:
    cpu_total: int = 4
    cpu_available: int = 4
    memory_total: int = 8192    # MB
    memory_available: int = 8192
    disk_total: int = 51200     # MB
    disk_available: int = 51200
    has_gpu: bool = False

    def to_dict(self) -> dict:
        return {
            "cpu_total": self.cpu_total,
            "cpu_available": self.cpu_available,
            "memory_total": self.memory_total,
            "memory_available": self.memory_available,
            "disk_total": self.disk_total,
            "disk_available": self.disk_available,
            "has_gpu": self.has_gpu,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkerResources":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class WorkerState:
    worker_id: str
    last_heartbeat: str = field(default_factory=_now_iso)
    status: WorkerStatus = WorkerStatus.IDLE
    capabilities: list[str] = field(default_factory=list)
    current_tasks: list[str] = field(default_factory=list)
    resources: WorkerResources = field(default_factory=WorkerResources)

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "last_heartbeat": self.last_heartbeat,
            "status": self.status.value,
            "capabilities": self.capabilities,
            "current_tasks": self.current_tasks,
            "resources": self.resources.to_dict(),
        }

    def save(self, path: Path) -> None:
        self.last_heartbeat = _now_iso()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(self.to_dict(), allow_unicode=True, sort_keys=False))

    @classmethod
    def from_dict(cls, d: dict) -> "WorkerState":
        return cls(
            worker_id=d["worker_id"],
            last_heartbeat=d.get("last_heartbeat", _now_iso()),
            status=WorkerStatus(d.get("status", "idle")),
            capabilities=d.get("capabilities", []),
            current_tasks=d.get("current_tasks", []),
            resources=WorkerResources.from_dict(d.get("resources", {})),
        )

    @classmethod
    def load(cls, path: Path) -> "WorkerState":
        return cls.from_dict(yaml.safe_load(path.read_text()))
