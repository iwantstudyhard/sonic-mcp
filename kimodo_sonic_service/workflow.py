"""生成、文件登记与推流的业务工作流。"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .backend import KimodoBackend, MotionBackend
from .config import ServiceConfig
from .stream import StreamManager

_TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
_ENDPOINT_PATTERN = re.compile(r"^tcp://(?:\*|127\.0\.0\.1|localhost):[1-9][0-9]{0,4}$")
_LOGGER = logging.getLogger(__name__)


class KimodoSonicWorkflow:
    """一次只执行一个 GPU 生成任务，避免模型并发耗尽显存。"""

    def __init__(
        self,
        config: ServiceConfig,
        backend: MotionBackend | None = None,
        stream_manager: StreamManager | None = None,
    ) -> None:
        self.config = config
        self.config.ensure_directories()
        self.backend = backend or KimodoBackend(
            config.model_name, config.device, config.source_fps
        )
        self.stream = stream_manager or StreamManager(config.publisher_script)
        self._generation_lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def _job_dir(self, job_id: str) -> Path:
        # job_id 只由服务端生成，所有输出严格位于 output_root 内。
        return self.config.output_root / job_id

    def _write_metadata(self, job: dict[str, Any]) -> None:
        path = self._job_dir(job["job_id"]) / "job.json"
        # 先写临时文件再替换，避免生成线程写入时，查询读到半个 JSON。
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def generate(
        self,
        prompt: str,
        duration_seconds: float = 5.0,
        denoising_steps: int = 100,
        seed: int = 0,
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt or len(prompt) > 500:
            raise ValueError("prompt 长度必须为 1~500 个字符")
        if not 0.1 <= duration_seconds <= self.config.max_duration_seconds:
            raise ValueError(
                f"duration_seconds 必须在 0.1~{self.config.max_duration_seconds:g} 之间"
            )
        if not 1 <= denoising_steps <= self.config.max_denoising_steps:
            raise ValueError(
                f"denoising_steps 必须在 1~{self.config.max_denoising_steps} 之间"
            )
        if not 0 <= seed <= 2**31 - 1:
            raise ValueError("seed 必须在 0~2147483647 之间")
        if not self._generation_lock.acquire(blocking=False):
            raise RuntimeError("已有 Kimodo 生成任务正在运行")

        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(mode=0o750)
        csv_path = job_dir / "motion.csv"
        npz_path = job_dir / "motion.npz"
        num_frames = max(1, round(duration_seconds * self.config.source_fps))
        job: dict[str, Any] = {
            "job_id": job_id,
            "status": "generating",
            "prompt": prompt,
            "duration_seconds": duration_seconds,
            "num_frames": num_frames,
            "denoising_steps": denoising_steps,
            "seed": seed,
            "created_at": time.time(),
        }
        self._jobs[job_id] = job
        self._write_metadata(job)
        _LOGGER.info("Kimodo 任务开始：job_id=%s", job_id)
        try:
            details = self.backend.generate_and_export(
                prompt, num_frames, denoising_steps, seed, csv_path, npz_path
            )
            if not csv_path.is_file() or not npz_path.is_file():
                raise RuntimeError("Kimodo 导出未生成完整的 CSV 和 NPZ 文件")
            job.update(
                status="ready",
                csv_file="motion.csv",
                npz_file="motion.npz",
                completed_at=time.time(),
                export=details,
            )
            self._write_metadata(job)
            _LOGGER.info("Kimodo 任务完成：job_id=%s，目录=%s", job_id, job_dir)
            return dict(job)
        except Exception as exc:
            job.update(status="failed", error=str(exc), completed_at=time.time())
            self._write_metadata(job)
            # 不仅向 MCP 客户端返回摘要，也在容器日志中保留完整异常堆栈。
            _LOGGER.exception("Kimodo 生成失败：job_id=%s", job_id)
            raise
        finally:
            self._generation_lock.release()

    def _ready_job(self, job_id: str) -> tuple[dict[str, Any], Path, Path]:
        job = self.job_status(job_id)
        if job["status"] != "ready":
            raise ValueError(f"任务 {job_id} 尚未就绪")
        job_dir = self._job_dir(job_id)
        csv_path = job_dir / "motion.csv"
        npz_path = job_dir / "motion.npz"
        if not csv_path.is_file() or not npz_path.is_file():
            raise FileNotFoundError(f"任务 {job_id} 的输出文件不完整")
        return job, csv_path, npz_path

    def start_stream(
        self,
        job_id: str,
        loop: bool = False,
        endpoint: str = "tcp://*:5556",
        topic: str = "pose",
        rate: float = 50.0,
        wrist_source: str = "smpl",
    ) -> dict[str, Any]:
        _, csv_path, npz_path = self._ready_job(job_id)
        if not _ENDPOINT_PATTERN.fullmatch(endpoint):
            raise ValueError("endpoint 只允许本机 tcp 绑定，例如 tcp://*:5556")
        port = int(endpoint.rsplit(":", 1)[1])
        if port > 65535:
            raise ValueError("endpoint 端口必须小于等于 65535")
        if not _TOPIC_PATTERN.fullmatch(topic):
            raise ValueError("topic 只能包含 ASCII 字母、数字及 _ . : / -")
        if not 1.0 <= rate <= 240.0:
            raise ValueError("rate 必须在 1~240 Hz 之间")
        if wrist_source not in {"smpl", "csv"}:
            raise ValueError("wrist_source 必须是 smpl 或 csv")
        return self.stream.start(
            job_id,
            csv_path,
            npz_path,
            endpoint,
            topic,
            rate,
            self.config.source_fps,
            loop,
            wrist_source,
        )

    def generate_and_stream(self, **kwargs: Any) -> dict[str, Any]:
        stream_keys = {"loop", "endpoint", "topic", "rate", "wrist_source"}
        generation = {key: value for key, value in kwargs.items() if key not in stream_keys}
        stream = {key: value for key, value in kwargs.items() if key in stream_keys}
        job = self.generate(**generation)
        return {"job": job, "stream": self.start_stream(job["job_id"], **stream)}

    def job_status(self, job_id: str) -> dict[str, Any]:
        if job_id in self._jobs:
            return dict(self._jobs[job_id])
        metadata = self._job_dir(job_id) / "job.json"
        try:
            resolved = metadata.resolve(strict=True)
            resolved.relative_to(self.config.output_root.resolve())
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(f"找不到任务：{job_id}") from exc
        return json.loads(resolved.read_text(encoding="utf-8"))

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1~100 之间")
        jobs: list[dict[str, Any]] = []
        for metadata in self.config.output_root.glob("*/job.json"):
            try:
                jobs.append(json.loads(metadata.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(jobs, key=lambda item: item.get("created_at", 0), reverse=True)[:limit]

    def health(self) -> dict[str, Any]:
        return {
            "service": "ok",
            "kimodo_available": bool(getattr(self.backend, "available", True)),
            "model": self.config.model_name,
            "device": self.config.device,
            "source_fps": self.config.source_fps,
            "generation_running": self._generation_lock.locked(),
            "stream": self.stream.status(),
        }
