#!/usr/bin/env python3
"""通过 ZeroMQ 发布 G1 关节数据和 SMPL 数据。

关节 CSV 每行应包含 36 列：
  第 0~2 列   ：根部位置（当前协议不发送）
  第 3~6 列   ：根部四元数（仅用于输入校验）
  第 7~35 列  ：29 个关节位置（可选作为 6 个腕关节的数据源）

SMPL NPZ 应包含：
  local_rot_mats [帧数, 22, 3, 3] 根节点和 21 个身体关节的局部旋转

发布格式是 GR00T/SONIC ZMQ Protocol v3：
  topic 前缀 + 1280 字节定长 JSON header + 二进制字段数据
"""

from __future__ import annotations

import argparse
# SMPL skeleton / conversion constants follow NVIDIA GR00T-WholeBodyControl.
# See THIRD_PARTY_NOTICES.md and licenses/SONIC-LICENSE.txt for attribution.
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np
import zmq
from scipy.spatial.transform import Rotation, Slerp
from zmq.utils.monitor import recv_monitor_message


EXPECTED_COLUMNS = 36
HEADER_SIZE = 1280
DEFAULT_TOPIC = "pose"
JOINT_START = 7
JOINT_COUNT = 29
SMPL_BODY_JOINT_COUNT = 22
SMPL_JOINT_COUNT = 24
SMPL_POSE_COUNT = 21
DEFAULT_RATE_HZ = 50.0
DEFAULT_BATCH_SIZE = 5

# 标准 SMPL 前 22 个关节的父节点关系，与官方 compute_human_joints 一致。
SMPL_PARENTS = np.asarray(
    (-1, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9, 9, 12, 13, 14, 16, 17, 18, 19),
    dtype=np.intp,
)

# 官方 human_joints_info.pkl 中标准 SMPL 骨架的前 22 个静态关节。
# 使用这套骨架重建关节，避免把输入 NPZ 的 Y-up 世界坐标直接送给编码器。
SMPL_REST_JOINTS = np.asarray(
    (
        (0.003123261, -0.351407468, 0.012036551),
        (0.061312653, -0.444170952, -0.013964635),
        (-0.060144216, -0.455315471, -0.009213820),
        (0.000360562, -0.241516858, -0.015581081),
        (0.116008110, -0.822924376, -0.023360699),
        (-0.104354173, -0.817695558, -0.026037702),
        (0.009808261, -0.109663606, -0.021521065),
        (0.072554663, -1.225983858, -0.055236645),
        (-0.088937365, -1.228423357, -0.046229973),
        (-0.001522153, -0.057428449, 0.006925832),
        (0.119811967, -1.283981204, 0.062979683),
        (-0.127749771, -1.286751747, 0.072819024),
        (-0.013686611, 0.107738607, -0.024689510),
        (0.044842005, 0.027515274, -0.000294651),
        (-0.049217079, 0.026910223, -0.006474070),
        (0.011096873, 0.268190414, -0.003952245),
        (0.164081037, 0.085243300, -0.015755590),
        (-0.151794821, 0.080434673, -0.019142598),
        (0.418203890, 0.013092781, -0.058214445),
        (-0.422944367, 0.043942191, -0.045609683),
        (0.670190632, 0.036314011, -0.060686525),
        (-0.672211826, 0.039409645, -0.060934868),
    ),
    dtype=np.float64,
)

# 官方 24 关节输出的末两点是 SMPL 索引 39/54 的左右拇指末端。
# 手指局部旋转为零时，末端相对腕部的偏移可沿父链合并为以下两个向量。
SMPL_LEFT_THUMB_OFFSET = np.asarray((0.07817459, -0.022160467, 0.06611209))
SMPL_RIGHT_THUMB_OFFSET = np.asarray((-0.07615411, -0.025255527, 0.06636047))

# 官方处理：SMPL 根节点由 Y-up 转成 Z-up，再移除 SMPL 静态基准旋转。
Y_UP_TO_Z_UP = Rotation.from_rotvec((np.pi / 2.0, 0.0, 0.0))
SMPL_BASE_ROTATION = Rotation.from_quat((0.5, 0.5, 0.5, 0.5))
WRIST_INDICES = np.asarray((23, 24, 25, 26, 27, 28), dtype=np.intp)

# Kimodo G1 CSV 使用 MuJoCo qpos 顺序；SONIC 要求 IsaacLab 顺序。
# 数组的第 i 项表示 IsaacLab 第 i 个关节在 MuJoCo 数组中的下标。
MUJOCO_TO_ISAACLAB = np.asarray(
    (
        0, 6, 12, 1, 7, 13, 2, 8, 14, 3,
        9, 15, 22, 4, 10, 16, 23, 5, 11, 17,
        24, 18, 25, 19, 26, 20, 27, 21, 28,
    ),
    dtype=np.intp,
)


def load_joint_motion(
    csv_path: Path,
    input_order: str,
) -> tuple[np.ndarray, np.ndarray]:
    """读取 CSV 的根部四元数和关节位置，并转换为 IsaacLab 顺序。"""
    positions: list[list[float]] = []
    body_quats: list[list[float]] = []

    with csv_path.open("r", newline="") as csv_file:
        reader = csv.reader(csv_file)
        for line_number, row in enumerate(reader, start=1):
            # 空行不属于动作数据，直接跳过。
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) != EXPECTED_COLUMNS:
                raise ValueError(
                    f"{csv_path}:{line_number}: 应有 {EXPECTED_COLUMNS} 列，"
                    f"实际为 {len(row)} 列"
                )
            try:
                values = [float(cell) for cell in row]
            except ValueError as exc:
                raise ValueError(
                    f"{csv_path}:{line_number}: 所有字段都必须是数字"
                ) from exc

            body_quats.append(values[3:7])
            positions.append(values[JOINT_START : JOINT_START + JOINT_COUNT])

    if not positions:
        raise ValueError(f"{csv_path}: 没有找到数据行")

    joint_pos = np.asarray(positions, dtype=np.float64)
    body_quat = np.asarray(body_quats, dtype=np.float64)
    if input_order == "mujoco":
        joint_pos = joint_pos[:, MUJOCO_TO_ISAACLAB]
    if not np.all(np.isfinite(joint_pos)) or not np.all(np.isfinite(body_quat)):
        raise ValueError(f"{csv_path}: 数据中包含 NaN 或无穷大")

    # 归一化 wxyz 四元数，避免输入精度误差污染姿态观测。
    norms = np.linalg.norm(body_quat, axis=1, keepdims=True)
    if np.any(norms <= 1e-12):
        raise ValueError(f"{csv_path}: body_quat 不能是零四元数")
    return joint_pos, body_quat / norms


def load_smpl_motion(
    npz_path: Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """读取局部旋转并生成官方 v3 语义的关节、轴角和根部四元数。"""
    try:
        with np.load(npz_path, allow_pickle=False) as data:
            missing = {"local_rot_mats"} - set(data.files)
            if missing:
                raise ValueError(
                    f"{npz_path}: 缺少字段 {', '.join(sorted(missing))}"
                )
            local_rot_mats = np.asarray(data["local_rot_mats"], dtype=np.float64)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(str(npz_path)):
            raise
        raise ValueError(f"无法读取 SMPL NPZ：{npz_path}: {exc}") from exc

    if local_rot_mats.ndim != 4 or local_rot_mats.shape[1:] != (22, 3, 3):
        raise ValueError(
            f"{npz_path}: local_rot_mats 应为 [N, 22, 3, 3]，"
            f"实际为 {list(local_rot_mats.shape)}"
        )
    if len(local_rot_mats) == 0:
        raise ValueError(f"{npz_path}: 没有动作帧")
    if not np.all(np.isfinite(local_rot_mats)):
        raise ValueError(f"{npz_path}: 数据中包含 NaN 或无穷大")

    determinants = np.linalg.det(local_rot_mats)
    if np.any(determinants <= 0.0) or np.max(np.abs(determinants - 1.0)) > 0.05:
        raise ValueError(f"{npz_path}: local_rot_mats 中存在无效旋转矩阵")

    # Rotation.from_matrix 会把轻微不正交的输入投影回合法旋转矩阵。
    rotations = Rotation.from_matrix(local_rot_mats.reshape(-1, 3, 3))
    clean_rot_mats = rotations.as_matrix().reshape(-1, 22, 3, 3)
    body_rotations = Rotation.from_matrix(clean_rot_mats[:, 1:].reshape(-1, 3, 3))
    smpl_pose = body_rotations.as_rotvec().reshape(-1, SMPL_POSE_COUNT, 3)

    # 只对根节点应用官方的 Y-up -> Z-up 变换；身体局部旋转保持 SMPL 定义。
    root_z_up = Y_UP_TO_Z_UP * Rotation.from_matrix(clean_rot_mats[:, 0])
    root_z_up_mats = root_z_up.as_matrix()

    # 使用标准 SMPL 静态骨架做前向运动学，等价于官方 compute_human_joints。
    relative_joints = SMPL_REST_JOINTS.copy()
    relative_joints[1:] -= SMPL_REST_JOINTS[SMPL_PARENTS[1:]]
    global_rotations = np.empty_like(clean_rot_mats)
    global_joints = np.empty((len(clean_rot_mats), 22, 3), dtype=np.float64)
    global_rotations[:, 0] = root_z_up_mats
    global_joints[:, 0] = np.einsum(
        "fij,j->fi", root_z_up_mats, relative_joints[0]
    )
    for joint_index in range(1, SMPL_BODY_JOINT_COUNT):
        parent = SMPL_PARENTS[joint_index]
        global_rotations[:, joint_index] = (
            global_rotations[:, parent] @ clean_rot_mats[:, joint_index]
        )
        global_joints[:, joint_index] = global_joints[:, parent] + np.einsum(
            "fij,j->fi", global_rotations[:, parent], relative_joints[joint_index]
        )

    smpl_joints_global = np.empty(
        (len(clean_rot_mats), SMPL_JOINT_COUNT, 3), dtype=np.float64
    )
    smpl_joints_global[:, :22] = global_joints
    smpl_joints_global[:, 22] = global_joints[:, 20] + np.einsum(
        "fij,j->fi", global_rotations[:, 20], SMPL_LEFT_THUMB_OFFSET
    )
    smpl_joints_global[:, 23] = global_joints[:, 21] + np.einsum(
        "fij,j->fi", global_rotations[:, 21], SMPL_RIGHT_THUMB_OFFSET
    )

    # 官方发送的是移除 SMPL 基准旋转后的 root-local 关节，而不是世界坐标。
    body_rotation = root_z_up * SMPL_BASE_ROTATION.inv()
    smpl_joints = np.einsum(
        "fji,fkj->fki", body_rotation.as_matrix(), smpl_joints_global
    )
    body_quat_xyzw = body_rotation.as_quat()
    body_quat_wxyz = body_quat_xyzw[:, (3, 0, 1, 2)]
    return smpl_joints, smpl_pose, body_quat_wxyz


def compute_smpl_wrist_joint_positions(smpl_pose: np.ndarray) -> np.ndarray:
    """按照官方 v3 streamer 的公式，从 SMPL 姿态生成 6 个 G1 腕关节。"""
    joint_pos = np.zeros((len(smpl_pose), JOINT_COUNT), dtype=np.float64)

    def elbow_swing_euler(rotvec: np.ndarray) -> np.ndarray:
        # 将肘部旋转分解为绕 Y 轴的 twist 和剩余 swing。
        quat_xyzw = Rotation.from_rotvec(rotvec).as_quat()
        twist_xyzw = np.zeros_like(quat_xyzw)
        twist_xyzw[:, 1] = quat_xyzw[:, 1]
        twist_xyzw[:, 3] = quat_xyzw[:, 3]
        norms = np.linalg.norm(twist_xyzw, axis=1, keepdims=True)
        singular = norms[:, 0] <= 1e-12
        twist_xyzw /= np.where(singular[:, None], 1.0, norms)
        # 180 度且旋转轴垂直于 Y 时 twist 不唯一，取单位旋转作为稳定退化值。
        twist_xyzw[singular] = (0.0, 0.0, 0.0, 1.0)
        swing = Rotation.from_quat(twist_xyzw).inv() * Rotation.from_quat(quat_xyzw)
        return swing.as_euler("XYZ", degrees=False)

    left_elbow = elbow_swing_euler(smpl_pose[:, 17])
    right_elbow = elbow_swing_euler(smpl_pose[:, 18])
    left_wrist = Rotation.from_rotvec(smpl_pose[:, 19]).as_euler("XYZ", degrees=False)
    right_wrist = Rotation.from_rotvec(smpl_pose[:, 20]).as_euler("XYZ", degrees=False)

    joint_pos[:, 23] = left_elbow[:, 0] + left_wrist[:, 0]
    joint_pos[:, 25] = left_wrist[:, 1]
    joint_pos[:, 27] = left_elbow[:, 2] + left_wrist[:, 2]
    joint_pos[:, 24] = -(right_elbow[:, 0] + right_wrist[:, 0])
    joint_pos[:, 26] = -right_wrist[:, 1]
    joint_pos[:, 28] = right_elbow[:, 2] + right_wrist[:, 2]
    return joint_pos


def _linear_resample(values: np.ndarray, source_times: np.ndarray, target_times: np.ndarray) -> np.ndarray:
    """沿时间轴对任意形状的数值数组做线性插值。"""
    flat_values = values.reshape(len(values), -1)
    result = np.empty((len(target_times), flat_values.shape[1]), dtype=np.float64)
    for column in range(flat_values.shape[1]):
        result[:, column] = np.interp(target_times, source_times, flat_values[:, column])
    return result.reshape((len(target_times),) + values.shape[1:])


def _rotation_resample(
    rotvecs: np.ndarray,
    source_times: np.ndarray,
    target_times: np.ndarray,
) -> np.ndarray:
    """逐关节使用球面插值重采样轴角，避免直接线性插值旋转。"""
    if len(rotvecs) == 1:
        return np.repeat(rotvecs, len(target_times), axis=0)
    result = np.empty((len(target_times),) + rotvecs.shape[1:], dtype=np.float64)
    for joint_index in range(rotvecs.shape[1]):
        rotations = Rotation.from_rotvec(rotvecs[:, joint_index])
        result[:, joint_index] = Slerp(source_times, rotations)(target_times).as_rotvec()
    return result


def resample_motion(
    joint_pos: np.ndarray,
    smpl_joints: np.ndarray,
    smpl_pose: np.ndarray,
    body_quat: np.ndarray,
    source_rate_hz: float,
    target_rate_hz: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """同步重采样全部数据，使一条 v3 消息中的各字段属于同一时刻。"""
    if source_rate_hz <= 0 or target_rate_hz <= 0:
        raise ValueError("source_rate_hz 和 target_rate_hz 必须大于 0")
    frame_counts = {len(joint_pos), len(body_quat), len(smpl_joints), len(smpl_pose)}
    if len(frame_counts) != 1:
        raise ValueError(
            "关节 CSV 与 SMPL NPZ 的帧数必须一致："
            f"CSV={len(joint_pos)}，NPZ={len(smpl_joints)}"
        )
    if len(joint_pos) == 1 or source_rate_hz == target_rate_hz:
        return joint_pos, smpl_joints, smpl_pose, body_quat

    last_time = (len(joint_pos) - 1) / source_rate_hz
    target_count = round(last_time * target_rate_hz) + 1
    source_times = np.arange(len(joint_pos), dtype=np.float64) / source_rate_hz
    target_times = np.minimum(
        np.arange(target_count, dtype=np.float64) / target_rate_hz,
        last_time,
    )

    # scipy 使用 xyzw，协议和 CSV 使用 wxyz，插值前后需要调整顺序。
    body_xyzw = body_quat[:, (1, 2, 3, 0)]
    resampled_xyzw = Slerp(source_times, Rotation.from_quat(body_xyzw))(
        target_times
    ).as_quat()
    return (
        _linear_resample(joint_pos, source_times, target_times),
        _linear_resample(smpl_joints, source_times, target_times),
        _rotation_resample(smpl_pose, source_times, target_times),
        resampled_xyzw[:, (3, 0, 1, 2)],
    )


def build_v3_packet(
    topic: str,
    frame_index: np.ndarray,
    body_quat: np.ndarray,
    joint_pos: np.ndarray,
    joint_vel: np.ndarray,
    smpl_joints: np.ndarray,
    smpl_pose: np.ndarray,
) -> bytes:
    """按照官方字段语义生成一条包含 N 帧的 v3 packed binary 消息。"""
    arrays = {
        "smpl_pose": np.asarray(smpl_pose),
        "smpl_joints": np.asarray(smpl_joints),
        "body_quat_w": np.asarray(body_quat),
        "joint_pos": np.asarray(joint_pos),
        "joint_vel": np.asarray(joint_vel),
        "frame_index": np.asarray(frame_index),
    }
    if arrays["frame_index"].ndim != 1 or len(arrays["frame_index"]) == 0:
        raise ValueError("frame_index 必须为非空的一维数组")
    frame_count = len(arrays["frame_index"])
    expected_shapes = {
        "smpl_pose": (frame_count, 21, 3),
        "smpl_joints": (frame_count, 24, 3),
        "body_quat_w": (frame_count, 4),
        "joint_pos": (frame_count, 29),
        "joint_vel": (frame_count, 29),
        "frame_index": (frame_count,),
    }
    for name, expected_shape in expected_shapes.items():
        if arrays[name].shape != expected_shape:
            raise ValueError(
                f"{name} 应为 {list(expected_shape)}，实际为 {list(arrays[name].shape)}"
            )
        if not np.all(np.isfinite(arrays[name])):
            raise ValueError(f"{name} 中包含 NaN 或无穷大")
    try:
        topic_bytes = topic.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("topic 必须只包含 ASCII 字符") from exc

    # 使用官方 Python streamer 的字段名称、类型和排列顺序。
    header = {
        "v": 3,
        "endian": "le",
        "count": 1,
        "fields": [
            {"name": "smpl_pose", "dtype": "f32", "shape": [frame_count, 21, 3]},
            {"name": "smpl_joints", "dtype": "f32", "shape": [frame_count, 24, 3]},
            {"name": "body_quat_w", "dtype": "f32", "shape": [frame_count, 4]},
            {"name": "joint_pos", "dtype": "f32", "shape": [frame_count, 29]},
            {"name": "joint_vel", "dtype": "f32", "shape": [frame_count, 29]},
            {"name": "frame_index", "dtype": "i64", "shape": [frame_count]},
        ],
    }
    header_bytes = json.dumps(
        header, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    if len(header_bytes) > HEADER_SIZE:
        raise ValueError(
            f"协议 JSON header 太长：{len(header_bytes)} 字节，最大为 {HEADER_SIZE}"
        )
    padded_header = header_bytes.ljust(HEADER_SIZE, b"\x00")

    # '<f4' 和 '<i8' 明确指定小端，确保与 header 的 le/f32/i64 一致。
    payload = b"".join(
        (
            arrays["smpl_pose"].astype("<f4", copy=False).tobytes(order="C"),
            arrays["smpl_joints"].astype("<f4", copy=False).tobytes(order="C"),
            arrays["body_quat_w"].astype("<f4", copy=False).tobytes(order="C"),
            arrays["joint_pos"].astype("<f4", copy=False).tobytes(order="C"),
            arrays["joint_vel"].astype("<f4", copy=False).tobytes(order="C"),
            arrays["frame_index"].astype("<i8", copy=False).tobytes(order="C"),
        )
    )
    return topic_bytes + padded_header + payload


def wait_for_subscriber(monitor: zmq.Socket, poller: zmq.Poller) -> int:
    """等待至少一个 SUB 接收端完成 ZeroMQ 握手。"""
    subscriber_count = 0
    print("等待仿真接收端连接...")
    while subscriber_count == 0:
        events = dict(poller.poll())
        if monitor not in events:
            continue
        event = recv_monitor_message(monitor)
        peer = event.get("endpoint", "未知地址")
        if isinstance(peer, bytes):
            peer = peer.decode(errors="replace")
        if event["event"] == zmq.EVENT_HANDSHAKE_SUCCEEDED:
            subscriber_count += 1
            print(f"接收端连接成功：{peer}")
    print("检测到接收端，开始发布数据。")
    return subscriber_count


def publish(
    csv_joint_pos: np.ndarray,
    smpl_joints: np.ndarray,
    smpl_pose: np.ndarray,
    body_quat: np.ndarray,
    endpoint: str,
    topic: str,
    rate_hz: float,
    source_rate_hz: float,
    batch_size: int,
    wrist_source: str,
    loop: bool,
    warmup: float,
) -> None:
    """同步重采样数据，并按官方滑动窗口方式发布 v3 消息。"""
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")
    csv_joint_pos, smpl_joints, smpl_pose, body_quat = resample_motion(
        csv_joint_pos,
        smpl_joints,
        smpl_pose,
        body_quat,
        source_rate_hz,
        rate_hz,
    )
    if batch_size > len(smpl_pose):
        raise ValueError("batch_size 不能超过动作总帧数")

    if wrist_source == "smpl":
        joint_pos = compute_smpl_wrist_joint_positions(smpl_pose)
    else:
        # v3 只使用 6 个腕关节；其余 23 个关节清零，避免与 SMPL 主运动冲突。
        joint_pos = np.zeros_like(csv_joint_pos)
        joint_pos[:, WRIST_INDICES] = csv_joint_pos[:, WRIST_INDICES]
    # 官方 v3 streamer 将 joint_vel 全部置零。
    joint_vel = np.zeros_like(joint_pos)

    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    socket.setsockopt(zmq.LINGER, 0)
    monitor_endpoint = f"inproc://g1-smpl-pub-monitor-{id(socket)}"
    socket.monitor(
        monitor_endpoint,
        zmq.EVENT_ACCEPTED
        | zmq.EVENT_HANDSHAKE_SUCCEEDED
        | zmq.EVENT_DISCONNECTED,
    )
    monitor = context.socket(zmq.PAIR)
    monitor.setsockopt(zmq.LINGER, 0)
    monitor.connect(monitor_endpoint)
    poller = zmq.Poller()
    poller.register(monitor, zmq.POLLIN)
    socket.bind(endpoint)

    period = 1.0 / rate_hz
    print(
        f"正在通过 {endpoint} 以 {rate_hz:g} Hz 发布 {len(joint_pos)} 帧 v3 数据，"
        f"滑动窗口为 {batch_size} 帧，腕关节来源为 {wrist_source}"
    )
    if warmup > 0:
        print(f"等待 {warmup:g} 秒，让接收端完成连接...")
        time.sleep(warmup)
    subscriber_count = wait_for_subscriber(monitor, poller)
    frame = 0
    next_send = time.monotonic()

    try:
        while True:
            # 及时处理连接变化；所有接收端断开后暂停，避免动作帧被白白丢弃。
            while subscriber_count > 0:
                monitor_events = dict(poller.poll(0))
                if monitor not in monitor_events:
                    break
                event = recv_monitor_message(monitor)
                peer = event.get("endpoint", "未知地址")
                if isinstance(peer, bytes):
                    peer = peer.decode(errors="replace")
                if event["event"] == zmq.EVENT_HANDSHAKE_SUCCEEDED:
                    subscriber_count += 1
                    print(f"接收端连接成功：{peer}")
                elif event["event"] == zmq.EVENT_DISCONNECTED:
                    subscriber_count = max(0, subscriber_count - 1)
                    print(f"接收端断开：{peer}")

            if subscriber_count == 0:
                print("当前没有接收端，暂停发布。")
                subscriber_count = wait_for_subscriber(monitor, poller)
                next_send = time.monotonic()
                continue

            # 等待窗口填满后再发送；之后每个时刻发送最近 N 帧。
            if frame >= batch_size - 1:
                global_indices = np.arange(frame - batch_size + 1, frame + 1)
                motion_indices = global_indices % len(joint_pos)
                packet = build_v3_packet(
                    topic=topic,
                    frame_index=global_indices.astype(np.int64),
                    body_quat=body_quat[motion_indices],
                    joint_pos=joint_pos[motion_indices],
                    joint_vel=joint_vel[motion_indices],
                    smpl_joints=smpl_joints[motion_indices],
                    smpl_pose=smpl_pose[motion_indices],
                )
                socket.send(packet)
            frame += 1
            if not loop and frame >= len(joint_pos):
                break

            next_send += period
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                # 发送落后时重置基准，避免瞬间补发大量过期帧。
                next_send = time.monotonic()
    except KeyboardInterrupt:
        print("\n已停止发布。")
    finally:
        monitor.close()
        socket.disable_monitor()
        socket.close()
        context.term()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过 ZeroMQ Protocol v3 发布 G1 关节和 SMPL 动作数据。"
    )
    parser.add_argument("csv_path", type=Path, help="36 列 G1 关节 CSV 文件")
    parser.add_argument(
        "smpl_path",
        type=Path,
        nargs="?",
        default=Path("dance.npz"),
        help="SMPL NPZ 文件（默认：dance.npz）",
    )
    parser.add_argument(
        "--input-order",
        choices=("mujoco", "isaaclab"),
        default="mujoco",
        help="CSV 关节顺序（默认：mujoco）",
    )
    parser.add_argument(
        "--endpoint",
        default="tcp://*:5556",
        help="ZeroMQ PUB 绑定地址（默认：tcp://*:5556）",
    )
    parser.add_argument(
        "--topic", default=DEFAULT_TOPIC, help="ZeroMQ 主题前缀（默认：pose）"
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=DEFAULT_RATE_HZ,
        help="发布频率 Hz（默认：50，与官方 streamer 一致）",
    )
    parser.add_argument(
        "--source-rate",
        type=float,
        default=30.0,
        help="CSV 和 NPZ 的原始采样率 Hz（默认：30）",
    )
    parser.add_argument("--loop", action="store_true", help="循环播放动作")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="每条消息携带的滑动窗口帧数（默认：5，与官方 streamer 一致）",
    )
    parser.add_argument(
        "--wrist-source",
        choices=("smpl", "csv"),
        default="smpl",
        help="6 个腕关节的数据源（默认：smpl，使用官方转换公式），也就是直接用 smpl 计算关节角度，真实的 csv 数据根本没使用",
    )
    parser.add_argument(
        "--warmup",
        type=float,
        default=1.0,
        help="绑定后等待接收端的秒数（默认：1）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        csv_joint_pos, _csv_body_quat = load_joint_motion(
            args.csv_path, args.input_order
        )
        smpl_joints, smpl_pose, body_quat = load_smpl_motion(args.smpl_path)
        publish(
            csv_joint_pos=csv_joint_pos,
            smpl_joints=smpl_joints,
            smpl_pose=smpl_pose,
            body_quat=body_quat,
            endpoint=args.endpoint,
            topic=args.topic,
            rate_hz=args.rate,
            source_rate_hz=args.source_rate,
            batch_size=args.batch_size,
            wrist_source=args.wrist_source,
            loop=args.loop,
            warmup=args.warmup,
        )
    except (OSError, ValueError, zmq.ZMQError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
