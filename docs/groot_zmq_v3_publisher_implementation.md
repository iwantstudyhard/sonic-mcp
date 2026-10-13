# GR00T / SONIC ZMQ Protocol v3 发布器实现说明

本文保留早期 `dance.csv` / `dance.npz` 的协议示例，这些样例文件不随发布包分发。当前 MCP 生成的是 `motion.npz` 与同步伴随 `motion.csv`，其 CSV 关节占位语义见 [README](../README.md#数据边界)。

## 1. 文档目的

本文说明本目录中 `publish_g1_smpl_data.py` 的设计和实现。该程序参考
`publish_g1_joint_data.py` 的 Protocol v1 发布方式，在保持相同 ZMQ wire format
和发布流程的基础上，增加 Protocol v3 要求的 SMPL 关节位置和身体姿态数据。

对应文件如下：

| 文件 | 用途 |
|---|---|
| `groot_zmq_stream_protocol.md` | v1/v3 协议字段和 wire format 说明 |
| `publish_g1_joint_data.py` | 原有 Protocol v1 关节发布器 |
| `publish_g1_smpl_data.py` | 本文描述的 Protocol v3 发布器 |
| `dance.csv` | G1 根部姿态和 29 个关节位置 |
| `dance.npz` | SMPL-H body 关节位置和局部旋转矩阵 |

Protocol v3 是 Joint + SMPL Combined 模式，对应接收端 encode mode `2`。

## 2. 运行环境

发布器使用 Python 3，依赖以下软件包：

```text
numpy
scipy
pyzmq
```

可以使用下面的命令安装依赖：

```bash
python3 -m pip install numpy scipy pyzmq
```

## 3. 输入数据

### 3.1 G1 关节 CSV

CSV 每一行表示一帧，共 36 列：

| CSV 下标 | 数量 | 内容 | 是否发送 |
|---:|---:|---|---|
| `0:3` | 3 | 根部 xyz 位置 | 否 |
| `3:7` | 4 | 根部四元数，顺序为 `w, x, y, z` | 仅校验；v3 根姿态改从 SMPL 生成 |
| `7:36` | 29 | G1 关节位置 | `--wrist-source csv` 时使用其中 6 个腕关节 |

发布器会检查每一行是否恰好包含 36 个数值，并拒绝 NaN、无穷大和零四元数。
根部四元数在发送前会归一化。

早期样例 `dance.csv` 的关节排列是 MuJoCo 顺序。程序默认使用下面的映射转换成
SONIC 要求的 IsaacLab 顺序：

```python
MUJOCO_TO_ISAACLAB = (
    0, 6, 12, 1, 7, 13, 2, 8, 14, 3,
    9, 15, 22, 4, 10, 16, 23, 5, 11, 17,
    24, 18, 25, 19, 26, 20, 27, 21, 28,
)
```

若输入 CSV 已经采用 IsaacLab 顺序，应使用：

```bash
--input-order isaaclab
```

### 3.2 SMPL NPZ

早期样例 `dance.npz` 共 180 帧，包含以下数组：

| 数组名 | 形状 | 类型 | 发布器用途 |
|---|---:|---|---|
| `posed_joints` | `[180, 22, 3]` | `float32` | 当前不发送，避免使用 Y-up 世界坐标 |
| `local_rot_mats` | `[180, 22, 3, 3]` | `float32` | 生成全部 v3 SMPL 字段 |
| `global_rot_mats` | `[180, 22, 3, 3]` | `float32` | 当前未使用 |
| `root_positions` | `[180, 3]` | `float32` | 当前未使用 |
| `foot_contacts` | `[180, 4]` | `bool` | 当前未使用 |

发布器只要求 `local_rot_mats`。加载时使用
`allow_pickle=False`，并检查数组形状、有限数值以及旋转矩阵的行列式。

## 4. SMPL 数据适配

### 4.1 `smpl_joints`：按官方骨架进行前向运动学

Protocol v3 要求：

```text
smpl_joints [N, 24, 3]
```

官方 streamer 不直接发送输入设备的世界坐标。它使用官方
`human_joints_info.pkl` 的标准 SMPL 静态骨架和局部旋转做前向运动学，并输出：

```text
SMPL 索引 0:22       22 个主要 body joints
SMPL 索引 39、54     左右拇指末端
```

本实现内置了这部分官方静态骨架数据，并从 `local_rot_mats` 重建相同的 24 点。
最后两个点不再复制左右腕位置。

根旋转先执行官方的 Y-up 到 Z-up 变换，然后移除 SMPL 静态基准旋转。24 个关节点
再通过该根旋转的逆变换转换到 root-local 坐标。发送的 `smpl_joints` 因此不包含
NPZ 的世界平移，并与 `body_quat_w` 使用同一根姿态来源。

### 4.2 `smpl_pose`：旋转矩阵转换成轴角

Protocol v3 要求：

```text
smpl_pose [N, 21, 3]
```

`local_rot_mats` 的索引 0 是根节点局部旋转，其余 21 项是身体关节局部旋转。
因此先移除根节点，再用 SciPy 将每个 `3 x 3` 旋转矩阵转换为三维轴角向量：

```text
local_rot_mats[:, 1:22]       [N, 21, 3, 3]
              ↓ Rotation.as_rotvec()
smpl_pose                       [N, 21, 3]
```

轴角向量的方向表示旋转轴，向量模长表示旋转角度，角度单位为弧度。

## 5. 帧同步和关节数据

### 5.1 帧数约束

关节 CSV 和 SMPL NPZ 描述同一段动作，因此原始帧数必须一致。程序在建立 ZMQ
连接前检查以下数组的第一维：

```text
joint_pos
body_quat_w
smpl_joints
smpl_pose
```

任一帧数不同都会停止运行，避免将不同时刻的关节和人体姿态装入同一条消息。

### 5.2 重采样

`--source-rate` 表示 CSV 和 NPZ 的原始采样率，`--rate` 表示发布频率。
当两个频率不相同时，四类数据使用相同的目标时间戳同步重采样：

| 数据 | 插值方式 |
|---|---|
| CSV 关节位置 | 线性插值，仅 `--wrist-source csv` 时使用 |
| `smpl_joints` | 线性插值 |
| `body_quat_w` | 四元数球面插值 SLERP |
| `smpl_pose` | 每个关节单独进行旋转 SLERP |

四元数在协议中采用 `wxyz` 顺序，而 SciPy 采用 `xyzw` 顺序。实现中会在
调用 SciPy 前后显式交换分量，网络数据仍然保持协议规定的 `wxyz`。

### 5.3 v3 关节数据

与官方 streamer 一致，`joint_pos` 的其他 23 个关节全部为零，只填写 IsaacLab
索引 `[23, 24, 25, 26, 27, 28]` 的左右腕 roll、pitch、yaw。默认根据 SMPL 肘部和
腕部旋转计算；使用 `--wrist-source csv` 时则从 CSV 取这 6 项。`joint_vel` 全部为
零，与官方 v3 实现保持一致。

## 6. Protocol v3 消息格式

### 6.1 整体布局

每次 `socket.send()` 发送一条 single-part ZMQ message，不使用 multipart：

```text
+----------------+------------------------+--------------------------+
| topic          | JSON header            | binary payload           |
| 默认 4 字节    | 固定 1280 字节         | 默认 3980 字节（N=5）    |
| "pose"         | JSON + NUL 填充        | 按 fields 顺序连续拼接   |
+----------------+------------------------+--------------------------+
```

默认 topic 为 `pose`，每条消息携带最近 5 帧，因此完整消息大小是：

```text
4 + 1280 + 3980 = 5264 字节
```

topic 后没有空格、换行或 NUL 分隔符。接收端必须先移除与订阅配置相同长度的
topic，之后的 1280 字节才是 header。

### 6.2 JSON header

发布器默认使用与官方相同的 5 帧滑动窗口，即 `N=5`。实际 header 内容为：

```json
{
  "v": 3,
  "endian": "le",
  "count": 1,
  "fields": [
    {"name": "smpl_pose", "dtype": "f32", "shape": [5, 21, 3]},
    {"name": "smpl_joints", "dtype": "f32", "shape": [5, 24, 3]},
    {"name": "body_quat_w", "dtype": "f32", "shape": [5, 4]},
    {"name": "joint_pos", "dtype": "f32", "shape": [5, 29]},
    {"name": "joint_vel", "dtype": "f32", "shape": [5, 29]},
    {"name": "frame_index", "dtype": "i64", "shape": [5]}
  ]
}
```

JSON 使用紧凑格式编码成 ASCII，随后在末尾填充 `0x00`，直到长度恰好为
1280 字节。

### 6.3 Payload 字段

所有浮点数组使用官方默认的 little-endian `float32`，帧号使用 `int64`：

| 顺序 | 字段 | dtype | 默认形状 | 字节数 | payload 偏移 |
|---:|---|---|---:|---:|---:|
| 1 | `smpl_pose` | `f32` | `[5, 21, 3]` | 1260 | 0 |
| 2 | `smpl_joints` | `f32` | `[5, 24, 3]` | 1440 | 1260 |
| 3 | `body_quat_w` | `f32` | `[5, 4]` | 80 | 2700 |
| 4 | `joint_pos` | `f32` | `[5, 29]` | 580 | 2780 |
| 5 | `joint_vel` | `f32` | `[5, 29]` | 580 | 3360 |
| 6 | `frame_index` | `i64` | `[5]` | 40 | 3940 |
| | **合计** | | | **3980** | |

字段顺序不能改变，因为 payload 本身没有字段名。接收端依据 header 中的顺序、
类型和形状计算切片位置。

当 topic 使用默认的 4 字节 `pose` 时，各区域在完整 ZMQ 消息中的绝对范围为：

```text
topic          [0, 4)
JSON header    [4, 1284)
smpl_pose      [1284, 2544)
smpl_joints    [2544, 3984)
body_quat_w    [3984, 4064)
joint_pos      [4064, 4644)
joint_vel      [4644, 5224)
frame_index    [5224, 5264)
```

## 7. 发布流程

程序的处理顺序如下：

```text
加载并检查 CSV
        |
        +--> 四元数归一化
        +--> MuJoCo 到 IsaacLab 关节映射
        |
加载并检查 NPZ
        |
        +--> 官方 SMPL 骨架前向运动学生成 24 个关节
        +--> Y-up 转 Z-up，再生成 root-local joints 和 body_quat_w
        +--> 21 个局部旋转矩阵转换为轴角
        |
检查 CSV/NPZ 帧数一致
        |
同步重采样全部字段
        |
根据 SMPL 生成 6 个腕关节，其余关节和 joint_vel 清零
        |
建立 ZMQ PUB 并等待 SUB 握手
        |
按 50 Hz 构建最近 5 帧的滑动窗口并发送
```

PUB 端会监控 ZMQ 握手事件。没有接收端时不会开始消耗动作帧；所有接收端断开
后，发布器会暂停并等待新的接收端。

## 8. 使用方法

在本目录运行：

```bash
cd <项目目录>
python3 publish_g1_smpl_data.py dance.csv dance.npz
```

默认配置：

```text
endpoint    tcp://*:5556
topic       pose
rate        50 Hz
source-rate 30 Hz
batch-size  5
wrist-source smpl
input-order mujoco
loop        关闭
```

循环播放动作：

```bash
python3 publish_g1_smpl_data.py dance.csv dance.npz --loop
```

指定端口、topic 和频率：

```bash
python3 publish_g1_smpl_data.py dance.csv dance.npz \
  --endpoint 'tcp://*:6000' \
  --topic pose \
  --source-rate 30 \
  --rate 50 \
  --loop
```

`smpl_path` 可以省略，默认读取当前工作目录下的 `dance.npz`：

```bash
python3 publish_g1_smpl_data.py dance.csv
```

发送端会等待接收端建立连接。正常启动时可看到：

```text
正在通过 tcp://*:5556 以 50 Hz 发布 299 帧 v3 数据，滑动窗口为 5 帧，腕关节来源为 smpl
等待仿真接收端连接...
接收端连接成功：...
检测到接收端，开始发布数据。
```

## 9. 接收端配置要求

接收端需要保证：

1. 使用 ZMQ `SUB` socket 连接发布地址，而不是再次 `bind` 相同地址。
2. 订阅的 topic 与发送端完全一致，默认是 ASCII 字符串 `pose`。
3. 使用 Protocol v3 / encode mode `2`。
4. 按 single-part 消息解析，不要调用 multipart 字段读取逻辑。
5. 移除 topic 后固定读取 1280 字节 header。
6. 根据 header 的 `fields` 顺序解析 little-endian payload。

如果发送端运行在另一台机器，接收端连接地址应使用发送机器的实际 IP，例如：

```text
tcp://<Kimodo宿主机可达IP>:5556
```

## 10. 已完成的验证

当前实现已针对 `dance.csv` 和 `dance.npz` 完成以下检查：

| 验证项目 | 结果 |
|---|---|
| CSV 数据形状 | `joint_pos [180, 29]` |
| NPZ 转换结果 | `smpl_joints [180, 24, 3]`、`smpl_pose [180, 21, 3]`、`body_quat_w [180, 4]` |
| header 版本 | `v=3` |
| header 字段顺序 | 与官方 streamer 及六个 payload 字段一致 |
| 默认 5 帧消息大小 | 5264 字节 |
| 默认 payload 大小 | 3980 字节 |
| payload 数值回读 | 与封包前数组一致 |
| 本地 ZMQ 实际收发 | 5 帧窗口连续重叠，首尾帧号正确 |
| 30 Hz 到 50 Hz 重采样 | 180 帧生成 299 帧，旋转和四元数有效 |

## 11. 常见问题

### 11.1 发布器一直显示等待接收端

这是预期行为。程序只有检测到 SUB 握手成功后才开始发送。检查接收程序是否已经
启动、IP 和端口是否正确，以及 SUB 是否订阅了相同 topic。

### 11.2 接收端无法解析 header

确认接收端先移除了 topic。默认情况下，header 从完整消息的第 4 字节开始，长度
固定为 1280 字节。解析 JSON 前需要去除末尾 NUL 填充。

### 11.3 字段出现错位或异常大数值

重点检查以下三点：

- 是否按 header 的字段顺序解析 payload；
- 是否使用 little-endian；
- `f32` 是否按每个元素 4 字节读取，`i64` 是否按 8 字节读取。

### 11.4 CSV 和 NPZ 帧数不同

当前实现要求两者严格一一对应，不会自动截断较长的数据源。应先在上游完成时间
对齐或裁剪，再运行发布器。

### 11.5 为什么没有发送 `root_positions`

本文依据的 Protocol v3 最小字段集合不包含根部位置。公共根部姿态由
`body_quat_w` 提供；`smpl_joints` 是官方定义的 root-local 骨架，不需要世界平移。
不能在未同步修改接收端解析器的情况下自行向 payload 插入额外字段。

## 12. 后续数据源替换注意事项

替换 `dance.npz` 时至少应确认：

- `local_rot_mats` 是局部旋转而不是全局旋转；
- `local_rot_mats` 的前 22 项采用标准 SMPL body 顺序；
- 旋转矩阵是右手正交矩阵；
- CSV 和 NPZ 来源于同一段动作并具有相同起始时刻；
- 数据源根旋转是 SMPL 的 Y-up 约定；其他约定需要调整根旋转转换。

只满足数组形状并不能保证动作语义正确。坐标系、关节顺序和时间同步必须同时
满足接收端约定。
