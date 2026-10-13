"""可远程连接的 Streamable HTTP MCP 服务。"""

from __future__ import annotations

import atexit
import asyncio
import hmac
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from .config import ServiceConfig
from .workflow import KimodoSonicWorkflow


class StaticTokenVerifier(TokenVerifier):
    """验证部署者预先配置的单个 Bearer Token。"""

    def __init__(self, expected_token: str) -> None:
        self.expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self.expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="kimodo-remote-client",
            scopes=["kimodo:control"],
        )


def create_mcp_server(
    config: ServiceConfig,
    workflow: KimodoSonicWorkflow | None = None,
) -> FastMCP:
    """创建 MCP 服务；传入 workflow 可用于测试或定制部署。"""
    controller = workflow or KimodoSonicWorkflow(config)

    # 解释器正常退出或收到 Ctrl+C 时，不遗留占用 ZMQ 端口的发布进程。
    atexit.register(controller.stream.stop)

    auth: AuthSettings | None = None
    verifier: StaticTokenVerifier | None = None
    if config.bearer_token:
        base_url = config.public_base_url
        auth = AuthSettings(
            issuer_url=base_url,
            resource_server_url=f"{base_url}/mcp",
            required_scopes=["kimodo:control"],
        )
        verifier = StaticTokenVerifier(config.bearer_token)

    mcp = FastMCP(
        name="kimodo-sonic-control",
        instructions=(
            "本 MCP 在运行 Kimodo 的主机上生成 SMPL-X 动作，导出 motion.npz 和"
            "同步 motion.csv，并控制 SONIC ZMQ Protocol v3 推流。推荐先调用 "
            "initialize，再调用 workflow_generate_and_stream；也可以依次调用 "
            "kimodo_generate 和 sonic_stream_start。默认动作源为 30 Hz，发布为 "
            "50 Hz、topic=pose、腕关节来源=smpl。sonic_stream_stop 用于停止推流，"
            "job_status/list_jobs 用于查询任务。服务未配置 KIMODO_MCP_TOKEN 时不鉴权。"
        ),
        host=config.host,
        port=config.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        auth=auth,
        token_verifier=verifier,
    )

    @mcp.tool(
        name="initialize",
        description=(
            "初始化并检查 Kimodo SONIC MCP 服务。返回服务能力、默认参数、"
            "鉴权状态和当前健康状态；适合在 MCP Inspector 中首先调用。"
        ),
    )
    def initialize_tool() -> dict[str, Any]:
        """提供一个可手动调用的初始化工具；协议级 initialize 仍由 MCP SDK 处理。"""
        return {
            "initialized": True,
            "service": "kimodo-sonic-control",
            "transport": "streamable-http",
            "mcp_path": "/mcp",
            "authentication": "bearer" if config.bearer_token else "none",
            "capabilities": {
                "kimodo_generation": True,
                "smpl_npz_export": True,
                "companion_csv_export": True,
                "sonic_zmq_v3_stream": True,
            },
            "defaults": {
                "source_fps": config.source_fps,
                "publish_rate": 50.0,
                "topic": "pose",
                "endpoint": "tcp://*:5556",
                "wrist_source": "smpl",
            },
            "recommended_sequence": [
                "initialize",
                "workflow_generate_and_stream",
                "sonic_stream_stop",
            ],
            "health": controller.health(),
        }

    @mcp.tool(description="检查服务、Kimodo 模型环境及 SONIC 推流状态。")
    def service_health() -> dict[str, Any]:
        return controller.health()

    @mcp.tool(
        description=(
            "用文本提示生成一个30 Hz SMPL-X动作，导出 motion.npz 与36列同步"
            " motion.csv；返回的 job_id 可传给 sonic_stream_start。"
        )
    )
    async def kimodo_generate(
        prompt: str,
        duration_seconds: float = 5.0,
        denoising_steps: int = 100,
        seed: int = 0,
    ) -> dict[str, Any]:
        # 模型加载、文本编码和生成都是阻塞操作，不能占用 MCP 的事件循环。
        # 仍等待生成完成并返回原有结果，其他连接和任务查询可以同时响应。
        return await asyncio.to_thread(
            controller.generate, prompt, duration_seconds, denoising_steps, seed
        )

    @mcp.tool(
        description=(
            "启动已完成任务的 SONIC Protocol v3 推流；默认绑定容器所有网卡的"
            "5556端口，topic=pose、50 Hz、腕关节从SMPL姿态计算。"
        )
    )
    def sonic_stream_start(
        job_id: str,
        loop: bool = False,
        endpoint: str = "tcp://*:5556",
        topic: str = "pose",
        rate: float = 50.0,
        wrist_source: str = "smpl",
    ) -> dict[str, Any]:
        return controller.start_stream(
            job_id, loop, endpoint, topic, rate, wrist_source
        )

    @mcp.tool(description="停止当前 SONIC v3 推流进程。")
    def sonic_stream_stop() -> dict[str, Any]:
        return controller.stream.stop()

    @mcp.tool(
        description=(
            "推荐入口：一次完成 Kimodo 生成、NPZ/CSV导出和SONIC v3推流；"
            "调用前应确认SONIC已订阅对应ZMQ端口。"
        )
    )
    async def workflow_generate_and_stream(
        prompt: str,
        duration_seconds: float = 5.0,
        denoising_steps: int = 100,
        seed: int = 0,
        loop: bool = False,
        endpoint: str = "tcp://*:5556",
        topic: str = "pose",
        rate: float = 50.0,
        wrist_source: str = "smpl",
    ) -> dict[str, Any]:
        # 一键生成入口也必须移到工作线程，避免绕过上述非阻塞处理。
        return await asyncio.to_thread(
            controller.generate_and_stream,
            prompt=prompt,
            duration_seconds=duration_seconds,
            denoising_steps=denoising_steps,
            seed=seed,
            loop=loop,
            endpoint=endpoint,
            topic=topic,
            rate=rate,
            wrist_source=wrist_source,
        )

    @mcp.tool(description="查询单个生成任务的状态和导出文件名。")
    def job_status(job_id: str) -> dict[str, Any]:
        return controller.job_status(job_id)

    @mcp.tool(description="按创建时间倒序列出生成任务。")
    def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
        return controller.list_jobs(limit)

    return mcp
