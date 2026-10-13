from __future__ import annotations

import csv
import asyncio
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

from kimodo_sonic_service.config import ServiceConfig
from kimodo_sonic_service.backend import KimodoBackend
from kimodo_sonic_service.mcp_server import create_mcp_server
from kimodo_sonic_service.workflow import KimodoSonicWorkflow


class FakeBackend:
    available = True

    def generate_and_export(
        self,
        prompt: str,
        num_frames: int,
        denoising_steps: int,
        seed: int,
        csv_path: Path,
        npz_path: Path,
    ) -> dict[str, Any]:
        with csv_path.open("w", newline="") as handle:
            csv.writer(handle).writerows(np.zeros((num_frames, 36)))
        rotations = np.broadcast_to(np.eye(3), (num_frames, 22, 3, 3)).copy()
        np.savez_compressed(npz_path, local_rot_mats=rotations)
        return {"frames": num_frames, "fps": 30.0}


class FakeStreamManager:
    def __init__(self) -> None:
        self.started: dict[str, Any] | None = None

    def start(self, job_id: str, csv_path: Path, npz_path: Path, endpoint: str,
              topic: str, rate: float, source_rate: float, loop: bool,
              wrist_source: str) -> dict[str, Any]:
        self.started = locals() | {"self": None}
        return {"running": True, "job_id": job_id, "pid": 1234}

    def stop(self) -> dict[str, Any]:
        return {"running": False, "job_id": None, "pid": None}

    def status(self) -> dict[str, Any]:
        return {"running": self.started is not None}


class BlockingBackend(FakeBackend):
    """模拟正在等待文本编码的生成任务，验证 MCP 不会一起卡住。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_and_export(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.entered.set()
        if not self.release.wait(timeout=3.0):
            raise TimeoutError("测试中的模拟生成未被释放")
        return super().generate_and_export(*args, **kwargs)


class FakeModel:
    output_skeleton = object()

    def __call__(self, prompts: str, num_frames: int, num_denoising_steps: int,
                 return_numpy: bool, post_processing: bool) -> dict[str, np.ndarray]:
        rotations = np.broadcast_to(
            np.eye(3), (1, num_frames, 22, 3, 3)
        ).copy()
        roots = np.zeros((1, num_frames, 3))
        roots[0, :, 2] = np.arange(num_frames) * 0.01
        return {"local_rot_mats": rotations, "root_positions": roots}


class FakeUnbatchedModel(FakeModel):
    def __call__(self, prompts: str, num_frames: int, num_denoising_steps: int,
                 return_numpy: bool, post_processing: bool) -> dict[str, np.ndarray]:
        output = super().__call__(
            prompts, num_frames, num_denoising_steps, return_numpy, post_processing
        )
        return {
            "local_rot_mats": output["local_rot_mats"][0],
            "root_positions": output["root_positions"][0],
        }


class FakeAMASSConverter:
    def __init__(self, fps: float, skeleton: object) -> None:
        self.fps = fps

    def convert_save_npz(
        self, output: dict[str, np.ndarray], npz_path: str, z_up: bool
    ) -> None:
        frames = output["local_rot_mats"].shape[1]
        np.savez(
            npz_path,
            trans=np.zeros((frames, 3)),
            pose_body=np.zeros((frames, 63)),
            mocap_frame_rate=np.asarray(self.fps),
        )


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        script = root / "publisher.py"
        script.write_text("# test\n", encoding="ascii")
        self.config = ServiceConfig(
            output_root=root / "outputs",
            publisher_script=script,
            source_fps=30.0,
        )
        self.stream = FakeStreamManager()
        self.workflow = KimodoSonicWorkflow(
            self.config, backend=FakeBackend(), stream_manager=self.stream
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_generate_exports_and_registers_files(self) -> None:
        job = self.workflow.generate("a person waves", 1.0, 20, 7)
        job_dir = self.config.output_root / job["job_id"]
        self.assertEqual(job["status"], "ready")
        self.assertTrue((job_dir / "motion.csv").is_file())
        self.assertTrue((job_dir / "motion.npz").is_file())
        metadata = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["num_frames"], 30)

    def test_generate_then_start_v3_stream(self) -> None:
        job = self.workflow.generate("walk", 0.5, 10, 0)
        result = self.workflow.start_stream(job["job_id"])
        self.assertTrue(result["running"])
        self.assertEqual(self.stream.started["topic"], "pose")
        self.assertEqual(self.stream.started["endpoint"], "tcp://*:5556")
        self.assertEqual(self.stream.started["rate"], 50.0)
        self.assertEqual(self.stream.started["source_rate"], 30.0)
        self.assertEqual(self.stream.started["wrist_source"], "smpl")

    def test_rejects_untrusted_endpoint_and_unknown_job(self) -> None:
        job = self.workflow.generate("walk", 0.5, 10, 0)
        with self.assertRaises(ValueError):
            self.workflow.start_stream(job["job_id"], endpoint="tcp://example.com:5556")
        with self.assertRaises(ValueError):
            self.workflow.job_status("../../etc")

    def test_rejects_out_of_range_generation(self) -> None:
        with self.assertRaises(ValueError):
            self.workflow.generate("", 1.0, 10, 0)
        with self.assertRaises(ValueError):
            self.workflow.generate("walk", 99.0, 10, 0)

    def test_lan_listener_allows_missing_token(self) -> None:
        config = ServiceConfig(
            host="0.0.0.0",
            output_root=self.config.output_root,
            publisher_script=self.config.publisher_script,
        )
        config.validate()

    def test_short_optional_token_is_rejected(self) -> None:
        config = ServiceConfig(
            host="0.0.0.0",
            bearer_token="short",
            output_root=self.config.output_root,
            publisher_script=self.config.publisher_script,
        )
        with self.assertRaises(ValueError):
            config.validate()

    def test_initialize_tool_is_visible_and_callable(self) -> None:
        server = create_mcp_server(self.config, self.workflow)

        async def inspect_initialize() -> tuple[list[str], dict[str, Any]]:
            tools = await server.list_tools()
            result = await server.call_tool("initialize", {})
            return [tool.name for tool in tools], result

        tool_names, result = asyncio.run(inspect_initialize())
        # 不同 MCP SDK 版本可能只返回内容列表，也可能返回（内容，结构化结果）。
        content = result[0] if isinstance(result, tuple) else result
        payload = json.loads(content[0].text)
        self.assertIn("initialize", tool_names)
        self.assertTrue(payload["initialized"])
        self.assertEqual(payload["defaults"]["topic"], "pose")
        self.assertEqual(payload["defaults"]["endpoint"], "tcp://*:5556")
        self.assertEqual(payload["authentication"], "none")

    def test_generation_tools_keep_other_mcp_requests_responsive(self) -> None:
        # 两个生成入口都要覆盖，防止一键推流仍使用旧的同步路径。
        for tool_name in ("kimodo_generate", "workflow_generate_and_stream"):
            with self.subTest(tool=tool_name):
                backend = BlockingBackend()
                workflow = KimodoSonicWorkflow(
                    self.config, backend=backend, stream_manager=self.stream
                )
                server = create_mcp_server(self.config, workflow)

                def payload(result: Any) -> dict[str, Any]:
                    content = result[0] if isinstance(result, tuple) else result
                    return json.loads(content[0].text)

                async def check_concurrent_requests() -> None:
                    generation = asyncio.create_task(server.call_tool(tool_name, {
                        "prompt": "walk", "duration_seconds": 1.0,
                        "denoising_steps": 10,
                    }))
                    try:
                        started = await asyncio.wait_for(
                            asyncio.to_thread(backend.entered.wait, 1.0), timeout=1.5
                        )
                        self.assertTrue(started)
                        self.assertFalse(generation.done())
                        for name in ("initialize", "service_health", "list_jobs"):
                            result = await asyncio.wait_for(
                                server.call_tool(name, {}), timeout=0.5
                            )
                            if name == "service_health":
                                self.assertTrue(payload(result)["generation_running"])
                        job = workflow.list_jobs(1)[0]
                        status = await asyncio.wait_for(server.call_tool(
                            "job_status", {"job_id": job["job_id"]}
                        ), timeout=0.5)
                        self.assertEqual(payload(status)["status"], "generating")
                    finally:
                        backend.release.set()
                        result = await asyncio.wait_for(generation, timeout=2.0)
                    completed = payload(result)
                    if tool_name == "workflow_generate_and_stream":
                        completed = completed["job"]
                    self.assertEqual(completed["status"], "ready")

                asyncio.run(check_concurrent_requests())

    def test_generation_failure_is_saved_and_logged_with_traceback(self) -> None:
        with patch.object(self.workflow.backend, "generate_and_export",
                          side_effect=RuntimeError("模拟编码失败")), self.assertLogs(
            "kimodo_sonic_service.workflow", level="ERROR"
        ) as logs:
            with self.assertRaisesRegex(RuntimeError, "模拟编码失败"):
                self.workflow.generate("walk", 1.0, 10, 0)
        job = self.workflow.list_jobs(1)[0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "模拟编码失败")
        self.assertIn("Traceback", logs.output[0])
        self.assertFalse(self.workflow.health()["generation_running"])


class KimodoBackendTest(unittest.TestCase):
    def test_availability_probe_does_not_import_the_model(self) -> None:
        backend = KimodoBackend("test", "cpu", 30.0)
        with patch("kimodo_sonic_service.backend.importlib.util.find_spec",
                   return_value=object()) as probe:
            self.assertTrue(backend.available)
            probe.assert_called_once_with("kimodo")
        self.assertIsNone(backend._model)

    def test_one_smplx_result_writes_npz_and_companion_csv(self) -> None:
        fake_smplx = types.ModuleType("kimodo.exports.smplx")
        fake_smplx.AMASSConverter = FakeAMASSConverter
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "sys.modules", {"kimodo.exports.smplx": fake_smplx}
        ):
            root = Path(temporary)
            backend = KimodoBackend("test", "cpu", 30.0)
            backend._model = FakeModel()
            # 假模型测试不初始化真实 PyTorch/GPU，避免依赖部署机器的驱动状态。
            with patch.object(backend, "_set_seed"):
                details = backend.generate_and_export(
                    "wave", 10, 5, 0, root / "motion.csv", root / "motion.npz"
                )
            csv_data = np.loadtxt(root / "motion.csv", delimiter=",")
            with np.load(root / "motion.npz", allow_pickle=False) as archive:
                self.assertEqual(archive["local_rot_mats"].shape, (10, 22, 3, 3))
            self.assertEqual(csv_data.shape, (10, 36))
            self.assertTrue(np.allclose(csv_data[:, 3], 1.0))
            self.assertTrue(np.allclose(csv_data[:, 7:], 0.0))
            self.assertEqual(details["csv_kind"], "smplx_v3_companion")

    def test_unbatched_smplx_result_is_batched_for_amass_export(self) -> None:
        fake_smplx = types.ModuleType("kimodo.exports.smplx")
        fake_smplx.AMASSConverter = FakeAMASSConverter
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "sys.modules", {"kimodo.exports.smplx": fake_smplx}
        ):
            root = Path(temporary)
            backend = KimodoBackend("test", "cpu", 30.0)
            backend._model = FakeUnbatchedModel()
            with patch.object(backend, "_set_seed"):
                backend.generate_and_export(
                    "walk", 10, 5, 0, root / "motion.csv", root / "motion.npz"
                )
            with np.load(root / "motion.npz", allow_pickle=False) as archive:
                self.assertEqual(archive["local_rot_mats"].shape, (10, 22, 3, 3))


if __name__ == "__main__":
    unittest.main()
