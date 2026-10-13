#!/usr/bin/env python3
"""通过 ZeroMQ 发布宇树 G1 的关节位置和关节速度数据。

输入 CSV 每行应包含 36 列：
  第 0~2 列   ：根部位置（当前脚本不发送）
  第 3~6 列   ：根部四元数 body_quat，顺序为 w、x、y、z
  第 7~35 列  ：29 个关节位置 joint_pos（Kimodo G1 CSV 的 MuJoCo 顺序）

发布格式是 GR00T/SONIC ZMQ Protocol v1：
  topic 前缀 + 1280 字节定长 JSON header + 二进制字段数据

Protocol v1 的必填字段为：
  body_quat   [N, 4]
  frame_index [N]
  joint_pos   [N, 29]
  joint_vel   [N, 29]
"""

from __future__ import annotations

import argparse
import csv
import json
import struct
import sys
import time
from pathlib import Path
from typing import Sequence

import zmq
from zmq.utils.monitor import recv_monitor_message


EXPECTED_COLUMNS = 36
HEADER_SIZE = 1280
DEFAULT_TOPIC = "pose"
JOINT_START = 7
JOINT_COUNT = 29

# Kimodo G1 CSV 使用 MuJoCo qpos 顺序；SONIC v1 要求 IsaacLab 顺序。
# 该数组表示：输出的 IsaacLab 第 i 个关节，取 CSV/MuJoCo 的哪个索引。
MUJOCO_TO_ISAACLAB = (
    0, 6, 12, 1, 7, 13, 2, 8, 14, 3,
    9, 15, 22, 4, 10, 16, 23, 5, 11, 17,
    24, 18, 25, 19, 26, 20, 27, 21, 28,
)

def load_motion_data(
    csv_path: Path,
    input_order: str,
) -> tuple[list[list[float]], list[list[float]]]:
    """读取 CSV 中的根部四元数和关节位置，并转换到 IsaacLab 顺序。"""
    positions: list[list[float]] = []
    body_quats: list[list[float]] = []

    with csv_path.open("r", newline="") as csv_file:
        reader = csv.reader(csv_file)
        for line_number, row in enumerate(reader, start=1):
            # 跳过空行。
            if not row or all(not cell.strip() for cell in row):
                continue

            # 当前 G1 文件应当是 36 列：根部位置、根部姿态和 29 个关节位置。
            if len(row) != EXPECTED_COLUMNS:
                raise ValueError(
                    f"{csv_path}:{line_number}: expected {EXPECTED_COLUMNS} "
                    f"columns, got {len(row)}"
                )

            try:
                values = [float(cell) for cell in row]
            except ValueError as exc:
                raise ValueError(
                    f"{csv_path}:{line_number}: 所有字段都必须是数字"
                ) from exc

            # 第 4~7 列（Python 下标 3~6）是 body_quat，顺序为 w、x、y、z。
            body_quats.append(values[3:7])
            # 第 8~36 列（Python 下标 7~35）是 29 个关节位置。
            raw_positions = values[JOINT_START : JOINT_START + JOINT_COUNT]
            if input_order == "mujoco":
                # Kimodo 导出的 G1 CSV 默认采用 MuJoCo qpos 顺序。
                joint_pos = [raw_positions[i] for i in MUJOCO_TO_ISAACLAB]
            else:
                joint_pos = raw_positions
            positions.append(joint_pos)

    if not positions:
        raise ValueError(f"{csv_path}: 没有找到数据行")

    return positions, body_quats


def normalize_quaternion(quat: list[float]) -> list[float]:
    """归一化 wxyz 四元数，避免输入的小数误差污染姿态观测。"""
    norm = sum(value * value for value in quat) ** 0.5
    if norm <= 1e-12:
        raise ValueError("body_quat 不能是零四元数")
    return [value / norm for value in quat]


def compute_joint_velocities(
    positions: list[list[float]],
    source_rate_hz: float,
) -> list[list[float]]:
    """根据源数据采样率计算关节速度，使用中心差分降低噪声。"""
    if source_rate_hz <= 0:
        raise ValueError("source_rate_hz 必须大于 0")
    if len(positions) == 1:
        return [[0.0] * JOINT_COUNT]

    dt = 1.0 / source_rate_hz
    velocities: list[list[float]] = []
    for frame_index in range(len(positions)):
        if frame_index == 0:
            previous = positions[0]
            following = positions[1]
            scale = 1.0 / dt
        elif frame_index == len(positions) - 1:
            previous = positions[-2]
            following = positions[-1]
            scale = 1.0 / dt
        else:
            previous = positions[frame_index - 1]
            following = positions[frame_index + 1]
            scale = 0.5 / dt

        velocities.append(
            [
                (following[joint_index] - previous[joint_index]) * scale
                for joint_index in range(JOINT_COUNT)
            ]
        )
    return velocities


def _slerp_wxyz(
    first: Sequence[float],
    second: Sequence[float],
    alpha: float,
) -> list[float]:
    """对 wxyz 四元数做最短路径球面插值。"""
    dot = sum(a * b for a, b in zip(first, second))
    second_values = list(second)
    if dot < 0.0:
        second_values = [-value for value in second_values]
        dot = -dot
    dot = max(-1.0, min(1.0, dot))
    if dot > 0.9995:
        result = [
            (1.0 - alpha) * a + alpha * b
            for a, b in zip(first, second_values)
        ]
        return normalize_quaternion(result)

    import math

    theta = math.acos(dot)
    sin_theta = math.sin(theta)
    first_weight = math.sin((1.0 - alpha) * theta) / sin_theta
    second_weight = math.sin(alpha * theta) / sin_theta
    return normalize_quaternion(
        [
            first_weight * a + second_weight * b
            for a, b in zip(first, second_values)
        ]
    )


def resample_motion(
    positions: list[list[float]],
    body_quats: list[list[float]],
    source_rate_hz: float,
    target_rate_hz: float,
) -> tuple[list[list[float]], list[list[float]]]:
    """把源动作插值到目标频率，保持原动作总时长不变。"""
    if source_rate_hz <= 0 or target_rate_hz <= 0:
        raise ValueError("source_rate_hz 和 target_rate_hz 必须大于 0")
    if len(positions) != len(body_quats):
        raise ValueError("joint_pos 和 body_quat 的帧数必须一致")
    if len(positions) <= 1 or source_rate_hz == target_rate_hz:
        return positions, body_quats

    import math

    source_last_time = (len(positions) - 1) / source_rate_hz
    target_count = round(source_last_time * target_rate_hz) + 1
    sampled_positions: list[list[float]] = []
    sampled_quats: list[list[float]] = []

    for target_index in range(target_count):
        source_time = min(target_index / target_rate_hz, source_last_time)
        source_index = min(
            int(math.floor(source_time * source_rate_hz)),
            len(positions) - 2,
        )
        alpha = source_time * source_rate_hz - source_index
        if source_time >= source_last_time:
            source_index = len(positions) - 2
            alpha = 1.0

        sampled_positions.append(
            [
                (1.0 - alpha) * first + alpha * second
                for first, second in zip(
                    positions[source_index],
                    positions[source_index + 1],
                )
            ]
        )
        sampled_quats.append(
            _slerp_wxyz(
                body_quats[source_index],
                body_quats[source_index + 1],
                alpha,
            )
        )

    return sampled_positions, sampled_quats


def build_v1_packet(
    topic: str,
    frame_index: int,
    body_quat: list[float],
    joint_pos: list[float],
    joint_vel: list[float],
) -> bytes:
    """按照 GR00T/SONIC ZMQ v1 生成一条 packed binary 消息。

    消息布局为：
      topic 前缀 + 1280 字节 JSON header + 按 fields 顺序排列的二进制数据

    每条消息发送 1 帧，因此所有带 N 的字段形状都是 [1, ...]。
    """
    if len(body_quat) != 4:
        raise ValueError("body_quat 必须包含 4 个数值")
    if len(joint_pos) != JOINT_COUNT or len(joint_vel) != JOINT_COUNT:
        raise ValueError("joint_pos 和 joint_vel 都必须包含 29 个数值")

    # v1 的公共必填字段：body_quat、frame_index。
    # v1 的协议字段：joint_pos、joint_vel。
    header = {
        "v": 1,
        "endian": "le",
        "count": 1,
        "fields": [
            {"name": "body_quat", "dtype": "f64", "shape": [1, 4]},
            {"name": "frame_index", "dtype": "i64", "shape": [1]},
            {"name": "joint_pos", "dtype": "f64", "shape": [1, 29]},
            {"name": "joint_vel", "dtype": "f64", "shape": [1, 29]},
        ],
    }
    header_bytes = json.dumps(
        header, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    if len(header_bytes) > HEADER_SIZE:
        raise ValueError(
            f"协议 JSON header 太长：{len(header_bytes)} 字节，最大为 {HEADER_SIZE}"
        )

    # 协议要求 header 固定为 1280 字节，不足部分用 NUL 填充。
    padded_header = header_bytes.ljust(HEADER_SIZE, b"\x00")

    # 按 header 中 fields 的顺序拼接 little-endian 二进制数组。
    payload = b"".join(
        (
            struct.pack("<4d", *body_quat),
            struct.pack("<q", frame_index),
            struct.pack("<29d", *joint_pos),
            struct.pack("<29d", *joint_vel),
        )
    )

    # 接收端会先剥离 topic 前缀，再读取 1280 字节 JSON header。
    return topic.encode("ascii") + padded_header + payload


def wait_for_subscriber(
    monitor: zmq.Socket,
    poller: zmq.Poller,
) -> int:
    """等待至少一个 SUB 接收端完成 ZeroMQ 握手。"""
    subscriber_count = 0
    print("等待仿真接收端连接...")
    while subscriber_count == 0:
        events = dict(poller.poll())
        if monitor not in events:
            continue

        event = recv_monitor_message(monitor)
        event_type = event["event"]
        peer = event.get("endpoint", "未知地址")
        if isinstance(peer, bytes):
            peer = peer.decode(errors="replace")

        if event_type == zmq.EVENT_HANDSHAKE_SUCCEEDED:
            subscriber_count += 1
            print(f"接收端连接成功：{peer}")

    print("检测到接收端，开始发布数据。")
    return subscriber_count


def publish(
    positions: list[list[float]],
    body_quats: list[list[float]],
    endpoint: str,
    topic: str,
    rate_hz: float,
    source_rate_hz: float,
    loop: bool,
    warmup: float,
) -> None:
    """按照指定频率循环发布关节位置和关节速度。"""
    if rate_hz <= 0:
        raise ValueError("rate_hz 必须大于 0")
    positions, body_quats = resample_motion(
        positions,
        body_quats,
        source_rate_hz,
        rate_hz,
    )
    velocities = compute_joint_velocities(positions, rate_hz)

    context = zmq.Context()
    # PUB 是发布端套接字。接收端需要使用 SUB，并连接到这个地址。
    socket = context.socket(zmq.PUB)
    socket.setsockopt(zmq.LINGER, 0)
    # 监控连接事件，用于判断 SUB 是否真正完成握手。
    monitor_endpoint = f"inproc://g1-pub-monitor-{id(socket)}"
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

    # 每帧之间的理论时间间隔，例如 30 Hz 对应 1/30 秒。
    period = 1.0 / rate_hz
    print(f"正在通过 {endpoint} 以 {rate_hz:g} Hz 发布 {len(positions)} 帧数据")
    if warmup > 0:
        print(f"等待 {warmup:g} 秒，让接收端完成连接...")
        time.sleep(warmup)

    subscriber_count = wait_for_subscriber(monitor, poller)

    frame = 0
    # 使用单调时钟控制发送节奏，避免系统时间调整影响帧率。
    next_send = time.monotonic()

    try:
        while True:
            # 已连接的 SUB 断开后，暂停发送并重新等待。
            # monitor 事件和发送同时处理，避免在没有接收端时继续丢帧。
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
                if event["event"] == zmq.EVENT_DISCONNECTED:
                    subscriber_count = max(0, subscriber_count - 1)
                    print(f"接收端断开：{peer}")

            if subscriber_count == 0:
                print("当前没有接收端，暂停发布。")
                subscriber_count = wait_for_subscriber(monitor, poller)
                next_send = time.monotonic()
                continue

            current_index = frame % len(positions)
            joint_pos = positions[current_index]
            body_quat = normalize_quaternion(body_quats[current_index])
            joint_vel = velocities[current_index]

            packet = build_v1_packet(
                topic=topic,
                frame_index=frame,
                body_quat=body_quat,
                joint_pos=joint_pos,
                joint_vel=joint_vel,
            )
            socket.send(packet)

            frame += 1
            if not loop and frame >= len(positions):
                break

            # 计算下一次发送时间，尽量保持稳定的 30 Hz。
            next_send += period
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                # 如果程序落后，重新建立发送时间基准，避免短时间内连续发送过多旧帧。
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
        description="通过 ZeroMQ 发布 CSV 中的 G1 joint_pos 和 joint_vel。"
    )
    parser.add_argument(
        "csv_path",
        type=Path,
        help="36 列 G1 CSV 文件的路径",
    )
    parser.add_argument(
        "--input-order",
        choices=("mujoco", "isaaclab"),
        default="mujoco",
        help="输入 CSV 的关节顺序（默认：mujoco，Kimodo G1 CSV 使用此顺序）",
    )
    parser.add_argument(
        "--endpoint",
        default="tcp://*:5556",
        help="ZeroMQ PUB 绑定地址（默认：tcp://*:5556）",
    )
    parser.add_argument(
        "--topic",
        default=DEFAULT_TOPIC,
        help="ZeroMQ 主题前缀（默认：pose）",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=30.0,
        help="发布频率，单位 Hz（默认：30）",
    )
    parser.add_argument(
        "--source-rate",
        type=float,
        default=30.0,
        help="CSV 原始采样率，单位 Hz，用于计算 joint_vel（默认：30）",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="循环播放 CSV 文件",
    )
    parser.add_argument(
        "--warmup",
        type=float,
        default=1.0,
        help="绑定后等待接收端连接的秒数（默认：1）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        positions, body_quats = load_motion_data(
            args.csv_path,
            input_order=args.input_order,
        )
        publish(
            positions=positions,
            body_quats=body_quats,
            endpoint=args.endpoint,
            topic=args.topic,
            rate_hz=args.rate,
            source_rate_hz=args.source_rate,
            loop=args.loop,
            warmup=args.warmup,
        )
    except (OSError, ValueError, zmq.ZMQError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
