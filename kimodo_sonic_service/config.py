"""服务配置与输入边界。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    """只允许通过环境变量修改受控参数，不接受远端传入任意命令。"""

    host: str = "0.0.0.0"
    port: int = 8000
    bearer_token: str | None = None
    public_base_url: str = "http://127.0.0.1:8000"
    output_root: Path = Path("kimodo_outputs")
    publisher_script: Path = Path("publish_g1_smpl_data.py")
    model_name: str = "Kimodo-SMPLX-RP-v1"
    device: str = "cuda"
    source_fps: float = 30.0
    max_duration_seconds: float = 30.0
    max_denoising_steps: int = 200

    @classmethod
    def from_env(cls, workspace: Path | None = None) -> "ServiceConfig":
        base = (workspace or Path.cwd()).resolve()
        output = Path(os.getenv("KIMODO_OUTPUT_ROOT", "kimodo_outputs"))
        script = Path(os.getenv("SONIC_PUBLISHER_SCRIPT", "publish_g1_smpl_data.py"))
        if not output.is_absolute():
            output = base / output
        if not script.is_absolute():
            script = base / script
        config = cls(
            host=os.getenv("KIMODO_MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("KIMODO_MCP_PORT", "8000")),
            bearer_token=os.getenv("KIMODO_MCP_TOKEN") or None,
            public_base_url=os.getenv(
                "KIMODO_MCP_PUBLIC_URL", "http://127.0.0.1:8000"
            ).rstrip("/"),
            output_root=output.resolve(),
            publisher_script=script.resolve(),
            model_name=os.getenv("KIMODO_MODEL", "Kimodo-SMPLX-RP-v1"),
            device=os.getenv("KIMODO_DEVICE", "cuda"),
            source_fps=float(os.getenv("KIMODO_SOURCE_FPS", "30")),
            max_duration_seconds=float(os.getenv("KIMODO_MAX_DURATION", "30")),
            max_denoising_steps=int(os.getenv("KIMODO_MAX_DENOISING_STEPS", "200")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("KIMODO_MCP_PORT 必须在 1~65535 之间")
        if self.source_fps <= 0 or self.max_duration_seconds <= 0:
            raise ValueError("帧率和最大动作时长必须大于 0")
        if self.max_denoising_steps <= 0:
            raise ValueError("最大去噪步数必须大于 0")
        # 局域网部署允许不配置 Token；设置 Token 后 MCP 服务会自动开启鉴权。
        if self.bearer_token is not None and len(self.bearer_token) < 24:
            raise ValueError("KIMODO_MCP_TOKEN 至少需要 24 个字符")

    def ensure_directories(self) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        if not self.publisher_script.is_file():
            raise FileNotFoundError(f"找不到 SONIC 推流器：{self.publisher_script}")
