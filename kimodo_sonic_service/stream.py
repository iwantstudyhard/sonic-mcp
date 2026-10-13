"""受控管理唯一的 SONIC v3 推流进程。"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


class StreamManager:
    def __init__(self, publisher_script: Path) -> None:
        self.publisher_script = publisher_script.resolve()
        self._process: subprocess.Popen[str] | None = None
        self._job_id: str | None = None
        self._lock = threading.Lock()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        process = self._process
        running = process is not None and process.poll() is None
        return {
            "running": running,
            "job_id": self._job_id if running else None,
            "pid": process.pid if running else None,
            "return_code": None if process is None or running else process.returncode,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def start(
        self,
        job_id: str,
        csv_path: Path,
        npz_path: Path,
        endpoint: str,
        topic: str,
        rate: float,
        source_rate: float,
        loop: bool,
        wrist_source: str,
    ) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError(f"已有推流任务正在运行：{self._job_id}")
            command = [
                sys.executable,
                str(self.publisher_script),
                str(csv_path),
                str(npz_path),
                "--input-order",
                "mujoco",
                "--endpoint",
                endpoint,
                "--topic",
                topic,
                "--rate",
                str(rate),
                "--source-rate",
                str(source_rate),
                "--wrist-source",
                wrist_source,
            ]
            if loop:
                command.append("--loop")
            # 不使用 shell，远端参数不会被解释为命令。
            self._process = subprocess.Popen(command, text=True)
            self._job_id = job_id
            return self._snapshot_unlocked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return self._snapshot_unlocked()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            return self._snapshot_unlocked()
