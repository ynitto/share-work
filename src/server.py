"""Unified HTTP server: task ordering (発注) API + embedded controller + worker."""

from __future__ import annotations

import http.server
import json
import logging
import os
import platform
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import psutil
import yaml

# ---------------------------------------------------------------------------
# Internal imports
# ---------------------------------------------------------------------------
from controller import Controller, DEFAULT_CONFIG as CTRL_DEFAULTS, load_config as _load_cfg
from worker import Worker, DEFAULT_CONFIG as WORKER_DEFAULTS
from models import TaskStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default config (controller + worker merged under one file)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: dict = {
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
    },
    "gitlab": {
        "repo_path": ".",
        "remote": "origin",
        "branch": "main",
    },
    "controller": {
        "interval": 60,
        "decompose_model": "claude-sonnet-4-6",
        "cleanup": {
            "enabled": True,
            "keep_failed_tasks": True,
            "artifacts_dir": "./collected_artifacts",
        },
        "timeouts": {
            "claim_ttl": 300,
            "execution_ttl": 3600,
        },
    },
    "worker": {
        "id": f"worker-{socket.gethostname()}",
        "interval": 30,
        "heartbeat_interval": 60,
        "max_concurrent_tasks": 3,
        "capabilities": [],
        "agent": {
            "binary": "claude",
            "model": "claude-sonnet-4-6",
            "timeout": 3600,
            "sandbox": True,
        },
        "resources": {
            "has_gpu": False,
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _build_controller_config(cfg: dict) -> dict:
    """Translate unified config → Controller config shape."""
    ctrl = cfg.get("controller", {})
    return {
        "gitlab": cfg["gitlab"],
        "polling": {
            "controller_interval": ctrl.get("interval", 60),
            "decompose_model": ctrl.get("decompose_model", "claude-sonnet-4-6"),
            "decompose_binary": ctrl.get("decompose_binary", "claude"),
        },
        "timeouts": ctrl.get("timeouts", {}),
        "cleanup": ctrl.get("cleanup", {}),
    }


def _build_worker_config(cfg: dict) -> dict:
    """Translate unified config → Worker config shape."""
    w = cfg.get("worker", {})
    agent = w.get("agent", {})
    return {
        "worker_id": w.get("id", f"worker-{socket.gethostname()}"),
        "gitlab": cfg["gitlab"],
        "polling": {
            "worker_interval": w.get("interval", 30),
            "heartbeat_interval": w.get("heartbeat_interval", 60),
        },
        "execution": {
            "max_concurrent_tasks": w.get("max_concurrent_tasks", 3),
            "agent_type": agent.get("type", "claude"),
            "agent_binary": agent.get("binary") or None,
            "agent_model": agent.get("model", "claude-sonnet-4-6"),
            "agent_timeout": agent.get("timeout", 3600),
            "agent_sandbox": agent.get("sandbox", True),
            "agent_suggestion_type": agent.get("suggestion_type", "shell"),
            "self_order_delay": w.get("self_order_delay", 0),
            "owner_ids": w.get("owner_ids", []),
        },
        "capabilities": w.get("capabilities", []),
        "resources": {
            **{
                "cpu_total": os.cpu_count() or 4,
                "memory_total": psutil.virtual_memory().total // (1024 * 1024),
                "disk_total": psutil.disk_usage("/").total // (1024 * 1024),
                "has_gpu": False,
            },
            **w.get("resources", {}),
        },
        # Disable built-in health server; the unified server handles HTTP
        "health": {"enabled": False},
    }


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class APIHandler(http.server.BaseHTTPRequestHandler):
    """Simple JSON REST API handler."""

    server: "TaskServer"  # type narrowing

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        qs = self._parse_qs()

        if path == "/health":
            self._json(self._health())
        elif path == "/metrics":
            self._text(self._metrics())
        elif path == "/tasks":
            statuses = None
            if "status" in qs:
                try:
                    statuses = [TaskStatus(s) for s in qs["status"].split(",")]
                except ValueError as e:
                    return self._error(400, str(e))
            try:
                tasks = self.server.controller.git.list_tasks(status=statuses)
                self._json([t.to_dict() for t in tasks])
            except Exception as e:
                self._error(500, str(e))
        elif m := re.fullmatch(r"/tasks/([^/]+)", path):
            task_id = m.group(1)
            meta_path = self.server.controller.git.meta_path(task_id)
            if not meta_path.exists():
                return self._error(404, f"Task {task_id} not found")
            from models import TaskMeta
            self._json(TaskMeta.load(meta_path).to_dict())
        elif path == "/workers":
            states = self.server.controller.git.list_worker_states()
            self._json([s.to_dict() for s in states])
        else:
            self._error(404, "Not found")

    def do_POST(self) -> None:
        path = self.path.rstrip("/")

        if path == "/tasks":
            body = self._read_json()
            if body is None:
                return
            requirement = body.get("requirement") or body.get("requirements", "")
            if not requirement:
                return self._error(400, "'requirement' is required")
            by = body.get("by", "http-client")
            repo_path = body.get("repo_path") or None
            mode = body.get("mode") or None
            if mode is not None and mode != "local":
                return self._error(400, f"'mode' must be 'local' or omitted, got '{mode}'")
            try:
                task_ids = self.server.controller.submit(
                    requirement, requested_by=by, repo_path=repo_path, mode=mode
                )
                local_accepted: list[str] = []
                if mode == "local":
                    for tid in task_ids:
                        if self.server.worker.claim_and_launch(tid):
                            local_accepted.append(tid)
                resp: dict = {"task_ids": task_ids}
                if mode == "local":
                    w = self.server.worker
                    with w._lock:
                        running = set(w._local_active)
                    resp["local_queued"] = [t for t in local_accepted if t not in running]
                    resp["local_running"] = [t for t in local_accepted if t in running]
                self._json(resp, status=201)
            except Exception as e:
                logger.error("submit error: %s", e)
                self._error(500, str(e))
        else:
            self._error(404, "Not found")

    def do_DELETE(self) -> None:
        path = self.path.rstrip("/")
        if m := re.fullmatch(r"/tasks/([^/]+)", path):
            task_id = m.group(1)
            meta_path = self.server.controller.git.meta_path(task_id)
            if not meta_path.exists():
                return self._error(404, f"Task {task_id} not found")
            from models import TaskMeta, TaskStatus
            meta = TaskMeta.load(meta_path)
            if meta.status in (TaskStatus.DONE, TaskStatus.FAILED):
                return self._error(409, f"Task already {meta.status.value}")
            meta.status = TaskStatus.CANCELLED
            meta.save(meta_path)
            try:
                self.server.controller.git.commit_and_push_with_retry(
                    f"task: cancel {task_id}", [meta_path]
                )
            except Exception as e:
                return self._error(500, str(e))
            self._json({"cancelled": task_id})
        else:
            self._error(404, "Not found")

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _json(self, body: Any, status: int = 200) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, body: str, status: int = 200) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def _read_json(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._error(400, "Empty request body")
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as e:
            self._error(400, f"Invalid JSON: {e}")
            return None

    def _parse_qs(self) -> dict[str, str]:
        if "?" not in self.path:
            return {}
        qs_str = self.path.split("?", 1)[1]
        result = {}
        for part in qs_str.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                result[k] = v
        return result

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("HTTP %s", fmt % args)

    # ------------------------------------------------------------------
    # Status data
    # ------------------------------------------------------------------

    def _health(self) -> dict:
        w = self.server.worker
        with w._lock:
            current_tasks = list(w._active_tasks.keys())
            local_queued = list(w._local_queue)
            local_active = list(w._local_active)
        return {
            "status": "ok",
            "worker_id": w.worker_id,
            "slots_free": w._semaphore._value,  # noqa: SLF001
            "slots_total": w.max_concurrent,
            "current_tasks": current_tasks,
            "local_queued": local_queued,
            "local_active": local_active,
        }

    def _metrics(self) -> str:
        w = self.server.worker
        avail = w._get_available_resources()
        wid = w.worker_id
        lines = [
            f'worker_cpu_available{{worker_id="{wid}"}} {avail["cpu"]}',
            f'worker_memory_available_mb{{worker_id="{wid}"}} {avail["memory"]}',
            f'worker_active_tasks{{worker_id="{wid}"}} {len(w._active_tasks)}',
            f'worker_slots_free{{worker_id="{wid}"}} {w._semaphore._value}',  # noqa: SLF001
        ]
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Unified Server
# ---------------------------------------------------------------------------

class TaskServer(http.server.ThreadingHTTPServer):
    """HTTP server that also runs Controller and Worker in background threads."""

    controller: Controller
    worker: Worker


def create_server(config: dict) -> TaskServer:
    cfg = _deep_merge(DEFAULT_CONFIG, config)

    ctrl_cfg = _build_controller_config(cfg)
    worker_cfg = _build_worker_config(cfg)

    controller = Controller(ctrl_cfg)
    worker = Worker(worker_cfg)

    host = cfg["server"].get("host", "0.0.0.0")
    port = cfg["server"].get("port", 8080)

    server = TaskServer((host, port), APIHandler)
    server.controller = controller
    server.worker = worker
    return server


def _run_controller(ctrl: Controller) -> None:
    logger.info("Controller loop started (interval=%ds)", ctrl.config["polling"]["controller_interval"])
    interval = ctrl.config["polling"]["controller_interval"]
    while ctrl._running:
        try:
            ctrl.run_once()
        except Exception as e:
            logger.error("Controller error: %s", e)
        time.sleep(interval)
    logger.info("Controller loop stopped")


def _run_worker(wkr: Worker) -> None:
    logger.info("Worker loop started (interval=%ds)", wkr.config["polling"]["worker_interval"])
    wkr.run()  # has its own loop
    logger.info("Worker loop stopped")


def start(config: dict) -> None:
    server = create_server(config)
    ctrl = server.controller
    wkr = server.worker

    ctrl._running = True
    wkr._running = True

    # Start controller + worker loops as daemon threads
    threading.Thread(target=_run_controller, args=(ctrl,), daemon=True, name="controller").start()
    threading.Thread(target=_run_worker, args=(wkr,), daemon=True, name="worker").start()

    host, port = server.server_address
    logger.info(
        "Task server listening on http://%s:%d",
        host if host != "0.0.0.0" else "localhost",
        port,
    )
    logger.info("  POST /tasks        - submit a task")
    logger.info("  GET  /tasks        - list tasks  (?status=open,claimed,...)")
    logger.info("  GET  /tasks/{id}   - task detail")
    logger.info("  DELETE /tasks/{id} - cancel task")
    logger.info("  GET  /workers      - worker states")
    logger.info("  GET  /health       - health check")
    logger.info("  GET  /metrics      - Prometheus metrics")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        ctrl._running = False
        wkr._running = False
        server.server_close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Distributed AI Task Server")
    parser.add_argument("--config", "-c", default="config/server.yaml", help="Config file path")
    parser.add_argument("--host", help="Bind host (overrides config)")
    parser.add_argument("--port", "-p", type=int, help="Bind port (overrides config)")
    args = parser.parse_args()

    cfg: dict = {}
    if Path(args.config).exists():
        with open(args.config) as f:
            cfg = yaml.safe_load(f) or {}
    else:
        logger.warning("Config file not found: %s, using defaults", args.config)

    if args.host:
        cfg.setdefault("server", {})["host"] = args.host
    if args.port:
        cfg.setdefault("server", {})["port"] = args.port

    start(cfg)


if __name__ == "__main__":
    main()
