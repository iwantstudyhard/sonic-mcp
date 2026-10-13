#!/usr/bin/env python3
"""从远端服务器调用本机 Kimodo + SONIC MCP 服务，属于是在一个客户端调用程序"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调用 Kimodo + SONIC MCP 服务")
    parser.add_argument(
        "--url", default=os.getenv("KIMODO_MCP_URL", "http://127.0.0.1:8000/mcp")
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health")

    generate = subparsers.add_parser("generate")
    generate.add_argument("prompt")
    generate.add_argument("--duration", type=float, default=5.0)
    generate.add_argument("--steps", type=int, default=100)
    generate.add_argument("--seed", type=int, default=0)

    start = subparsers.add_parser("stream-start")
    start.add_argument("job_id")
    start.add_argument("--loop", action="store_true")
    start.add_argument("--endpoint", default="tcp://*:5556")
    start.add_argument("--topic", default="pose")
    start.add_argument("--rate", type=float, default=50.0)
    start.add_argument("--wrist-source", choices=("smpl", "csv"), default="smpl")

    subparsers.add_parser("stream-stop")

    combined = subparsers.add_parser("generate-stream")
    combined.add_argument("prompt")
    combined.add_argument("--duration", type=float, default=5.0)
    combined.add_argument("--steps", type=int, default=100)
    combined.add_argument("--seed", type=int, default=0)
    combined.add_argument("--loop", action="store_true")
    combined.add_argument("--endpoint", default="tcp://*:5556")
    combined.add_argument("--topic", default="pose")
    combined.add_argument("--rate", type=float, default=50.0)
    return parser.parse_args()


def tool_request(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    if args.command == "health":
        return "service_health", {}
    if args.command == "stream-stop":
        return "sonic_stream_stop", {}
    if args.command == "generate":
        return "kimodo_generate", {
            "prompt": args.prompt,
            "duration_seconds": args.duration,
            "denoising_steps": args.steps,
            "seed": args.seed,
        }
    if args.command == "stream-start":
        return "sonic_stream_start", {
            "job_id": args.job_id,
            "loop": args.loop,
            "endpoint": args.endpoint,
            "topic": args.topic,
            "rate": args.rate,
            "wrist_source": args.wrist_source,
        }
    return "workflow_generate_and_stream", {
        "prompt": args.prompt,
        "duration_seconds": args.duration,
        "denoising_steps": args.steps,
        "seed": args.seed,
        "loop": args.loop,
        "endpoint": args.endpoint,
        "topic": args.topic,
        "rate": args.rate,
        "wrist_source": "smpl",
    }


async def invoke(args: argparse.Namespace) -> int:
    headers: dict[str, str] = {}
    token = os.getenv("KIMODO_MCP_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    tool_name, arguments = tool_request(args)
    async with httpx.AsyncClient(headers=headers, timeout=None) as http_client:
        async with streamable_http_client(args.url, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                payload = result.structuredContent
                if payload is None:
                    payload = [getattr(item, "text", str(item)) for item in result.content]
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 1 if result.isError else 0


def main() -> int:
    try:
        return asyncio.run(invoke(parse_args()))
    except Exception as exc:
        # MCP 客户端内部用 TaskGroup，HTTP 401 等错误可能包装为 ExceptionGroup。
        print(f"调用失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
