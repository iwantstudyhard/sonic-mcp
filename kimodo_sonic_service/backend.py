"""Kimodo 官方 Python API 的窄封装。"""

from __future__ import annotations

import inspect
import importlib.util
import logging
import random
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from scipy.spatial.transform import Rotation

_LOGGER = logging.getLogger(__name__)


class MotionBackend(Protocol):
    """便于测试替换的最小生成接口。"""

    def generate_and_export(
        self,
        prompt: str,
        num_frames: int,
        denoising_steps: int,
        seed: int,
        csv_path: Path,
        npz_path: Path,
    ) -> dict[str, Any]: ...


def _first_sample(value: Any, expected_unbatched_ndim: int) -> np.ndarray:
    array = np.asarray(value)
    if array.ndim == expected_unbatched_ndim + 1:
        array = array[0]
    if array.ndim != expected_unbatched_ndim:
        raise ValueError(f"Kimodo 输出维度异常：{array.shape}")
    return array


def _ensure_batch(value: Any, unbatched_shape: tuple[int, ...], name: str) -> np.ndarray:
    """将单样本输出统一为带 batch 维度的数组，供官方导出器使用。"""
    array = np.asarray(value)
    expected_unbatched_ndim = len(unbatched_shape)
    if array.ndim == expected_unbatched_ndim:
        if array.shape != unbatched_shape:
            raise ValueError(
                f"Kimodo 输出 {name} 应为 {unbatched_shape}，实际为 {array.shape}"
            )
        return array[None, ...]
    if array.ndim == expected_unbatched_ndim + 1:
        if array.shape[1:] != unbatched_shape:
            raise ValueError(
                f"Kimodo 输出 {name} 的单样本形状应为 {unbatched_shape}，"
                f"实际为 {array.shape}"
            )
        return array
    raise ValueError(
        f"Kimodo 输出 {name} 应为 {expected_unbatched_ndim}D 或 "
        f"{expected_unbatched_ndim + 1}D，实际为 {array.ndim}D"
    )


class KimodoBackend:
    """延迟加载模型，使服务启动和健康检查不依赖 GPU 初始化。"""

    def __init__(self, model_name: str, device: str, fps: float) -> None:
        self.model_name = model_name
        self.device = device
        self.fps = fps
        self._model: Any = None

    @property
    def available(self) -> bool:
        try:
            # 健康检查只探测安装情况，不导入模型；避免与工作线程争用导入锁。
            return importlib.util.find_spec("kimodo") is not None
        except (ImportError, ValueError):
            return False

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from kimodo import load_model
        except ImportError as exc:
            raise RuntimeError(
                "当前 Python 环境未安装 kimodo；请按 NVIDIA Kimodo Installation 文档安装"
            ) from exc
        _LOGGER.info("开始加载 Kimodo 模型：%s，设备：%s", self.model_name, self.device)
        self._model = load_model(self.model_name, device=self.device, eval_mode=True)
        _LOGGER.info("Kimodo 模型加载完成")
        return self._model

    @staticmethod
    def _set_seed(seed: int) -> None:
        random.seed(seed)
        np.random.seed(seed)
        try:
            import torch

            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
        except ImportError:
            # Kimodo 正常安装时会包含 torch；保留此分支便于环境诊断。
            pass

    def generate_and_export(
        self,
        prompt: str,
        num_frames: int,
        denoising_steps: int,
        seed: int,
        csv_path: Path,
        npz_path: Path,
    ) -> dict[str, Any]:
        model = self._load()
        self._set_seed(seed)
        try:
            from kimodo.exports.smplx import AMASSConverter
        except ImportError as exc:
            raise RuntimeError("Kimodo 安装不完整，缺少官方导出模块") from exc

        # API 参考页写作 prompts，快速入门示例写作 prompt；兼容两个官方版本。
        parameters = inspect.signature(model.__call__).parameters
        prompt_name = "prompts" if "prompts" in parameters else "prompt"
        # Kimodo 的 post_processing 内部要求 local_rot_mats 保持 5D。
        # 当前官方实现收到单字符串时会压掉 batch 维度，因此使用单元素
        # 列表表示一个样本，避免在模型内部提前触发 5D 断言。
        prompt_value: str | list[str] = [prompt] if prompt_name == "prompts" else prompt
        _LOGGER.info("开始生成动作：%s 帧，%s 步去噪", num_frames, denoising_steps)
        output = model(
            **{prompt_name: prompt_value},
            num_frames=num_frames,
            num_denoising_steps=denoising_steps,
            return_numpy=True,
            post_processing=True,
        )
        if not isinstance(output, dict):
            raise ValueError("Kimodo 模型没有返回动作字典")
        required = {"local_rot_mats", "root_positions"}
        missing = required - set(output)
        if missing:
            raise ValueError(f"Kimodo 输出缺少字段：{', '.join(sorted(missing))}")
        _LOGGER.info("动作生成完成，开始导出 SMPL NPZ 和同步 CSV")

        # 不同 Kimodo 版本返回的 local_rot_mats 可能是 4D 单样本，
        # 而 AMASSConverter 要求带 batch 维度的 5D 数组；这里在导出前统一。
        batched_output = dict(output)
        batched_output["local_rot_mats"] = _ensure_batch(
            output["local_rot_mats"], (num_frames, 22, 3, 3), "local_rot_mats"
        )
        batched_output["root_positions"] = _ensure_batch(
            output["root_positions"], (num_frames, 3), "root_positions"
        )

        amass = AMASSConverter(self.fps, model.output_skeleton)
        amass.convert_save_npz(batched_output, str(npz_path), z_up=True)

        # SONIC v3 推流器需要 Kimodo 的 22 关节局部旋转；旧版 AMASS 导出
        # 可能未保留该键，因此在官方导出完成后补入同一份生成结果。
        local_rot_mats = _first_sample(output["local_rot_mats"], 4)
        if local_rot_mats.shape != (num_frames, 22, 3, 3):
            raise ValueError(
                "SONIC 需要 local_rot_mats 为 "
                f"[{num_frames}, 22, 3, 3]，实际为 {local_rot_mats.shape}"
            )
        with np.load(npz_path, allow_pickle=False) as archive:
            arrays = {name: archive[name] for name in archive.files}
        arrays["local_rot_mats"] = local_rot_mats.astype(np.float32, copy=False)
        np.savez_compressed(npz_path, **arrays)

        # 官方不支持把 22 关节 SMPL-X 直接交给仅接受 G1 骨架的
        # MujocoQposConverter。v3 在 wrist_source=smpl 时只用 CSV 做同步帧容器，
        # 因而从同一个 SMPL-X 结果写出根位姿，并将 29 个机器人关节置零。
        root_positions = _first_sample(output["root_positions"], 2)
        kimodo_to_mujoco = np.asarray(
            ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        )
        root_translation = root_positions @ kimodo_to_mujoco.T
        root_rotation = kimodo_to_mujoco @ local_rot_mats[:, 0] @ kimodo_to_mujoco.T
        root_xyzw = Rotation.from_matrix(root_rotation).as_quat()
        qpos_companion = np.zeros((num_frames, 36), dtype=np.float32)
        qpos_companion[:, :3] = root_translation
        qpos_companion[:, 3:7] = root_xyzw[:, (3, 0, 1, 2)]
        np.savetxt(csv_path, qpos_companion, delimiter=",")

        return {
            "frames": num_frames,
            "fps": self.fps,
            "csv_columns": 36,
            "csv_kind": "smplx_v3_companion",
            "npz_keys": sorted(arrays),
        }
