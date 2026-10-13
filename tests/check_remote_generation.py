"""通过真实 HTTP 请求验证：生成动作期间，MCP 仍能处理其他请求。"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
import urllib.request
from typing import Any


def rpc(url: str, method: str, params: dict[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        }).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    # 局域网/Tailscale 测试直接连接服务，不经过系统代理。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        result = json.load(response)
    if "error" in result:
        raise RuntimeError(result["error"])
    result = result["result"]
    if result.get("isError"):
        raise RuntimeError(result.get("content"))
    return result


def tool(url: str, name: str, arguments: dict[str, Any], timeout: float) -> Any:
    result = rpc(url, "tools/call", {"name": name, "arguments": arguments}, timeout)
    if "structuredContent" in result:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--prompt", default="a person walks forward")
    args = parser.parse_args()
    started = time.monotonic()
    created_after = time.time()
    probes: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        generation = executor.submit(tool, args.url, "kimodo_generate", {
            "prompt": args.prompt, "duration_seconds": args.duration,
            "denoising_steps": args.steps, "seed": 0,
        }, args.timeout)
        while not generation.done():
            probe: dict[str, Any] = {"elapsed_seconds": round(time.monotonic() - started, 3)}
            for name, method, params in (
                ("protocol_initialize", "initialize", {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "nonblocking-check", "version": "1.0"},
                }),
                ("tools_list", "tools/list", {}),
                ("initialize_tool", "tools/call", {"name": "initialize", "arguments": {}}),
                ("service_health", "tools/call", {"name": "service_health", "arguments": {}}),
            ):
                before = time.monotonic()
                result = rpc(args.url, method, params, timeout=5.0)
                probe[f"{name}_ms"] = round((time.monotonic() - before) * 1000, 2)
                if name == "service_health":
                    health = result.get("structuredContent")
                    if health is None:
                        health = json.loads(result["content"][0]["text"])
                    probe["generation_running"] = health["generation_running"]
            before = time.monotonic()
            jobs = tool(args.url, "list_jobs", {"limit": 1}, 5.0)
            if isinstance(jobs, dict):
                jobs = jobs.get("result", [jobs])
            probe["list_jobs_ms"] = round((time.monotonic() - before) * 1000, 2)
            if jobs and jobs[0].get("created_at", 0) >= created_after - 1:
                before = time.monotonic()
                status = tool(args.url, "job_status", {"job_id": jobs[0]["job_id"]}, 5.0)
                probe["job_status_ms"] = round((time.monotonic() - before) * 1000, 2)
                probe["job_id"] = status["job_id"]
                probe["status"] = status["status"]
            probes.append(probe)
            print(json.dumps({"probe": probe}, ensure_ascii=False), flush=True)
            # 等待生成结果的同时，每隔三秒检查一次控制接口。
            concurrent.futures.wait([generation], timeout=3.0)
        job = generation.result()
    responsive_during_generation = any(p.get("generation_running") for p in probes)
    summary = {
        "job": job,
        "total_seconds": round(time.monotonic() - started, 3),
        "concurrent_probe_count": len(probes),
        "responsive_during_generation": responsive_during_generation,
        "max_probe_ms": max((v for p in probes for k, v in p.items()
                             if k.endswith("_ms")), default=0),
    }
    print(json.dumps({"summary": summary}, ensure_ascii=False), flush=True)
    return 0 if job.get("status") == "ready" and responsive_during_generation else 1


if __name__ == "__main__":
    raise SystemExit(main())
