# GR00T / SONIC ZMQ 流协议说明

本文整理 NVIDIA GR00T-WholeBodyControl / GEAR-SONIC 的 ZMQ motion stream 格式，重点说明 Protocol v1 和 Protocol v3。

本文区分两层内容：

1. **协议字段层**：网页文档规定每个协议版本必须提供哪些字段、字段形状和数据类型。
2. **ZMQ wire format 层**：官方 C++ 接收端规定一条 ZMQ 消息在网络上如何封装。

## 官方来源

- [ZMQ Streaming Motion Tracking 文档](https://nvlabs.github.io/GR00T-WholeBodyControl/tutorials/zmq.html)
- [ZMQ packed message subscriber](https://github.com/NVlabs/GR00T-WholeBodyControl/blob/0a87181c9106d0e49293400714b157676e0ec664/gear_sonic_deploy/src/g1/g1_deploy_onnx_ref/include/input_interface/zmq_packed_message_subscriber.hpp)
- [官方 ZMQ packed message 测试发送端](https://github.com/NVlabs/GR00T-WholeBodyControl/blob/0a87181c9106d0e49293400714b157676e0ec664/gear_sonic_deploy/src/g1/g1_deploy_onnx_ref/tests/zmq_pose_subscriber_test.cpp)

## 一、ZMQ 消息的整体封装

官方接收端按照一条 **single-part ZMQ message** 解析数据。整条消息的布局是：

```text
+------------------+----------------------+----------------------------+
| topic 前缀       | 1280 字节 JSON header | 二进制 payload             |
| 可为空           | NUL 填充到固定长度   | 按 fields 顺序连续排列    |
+------------------+----------------------+----------------------------+
```

这不是三条 ZMQ 消息，也不是三个 multipart frame，而是一条连续的消息字节流：

```text
[topic_prefix][fixed JSON header][field_0 bytes][field_1 bytes]...
```

### 1. Topic 前缀

默认 topic 是：

```text
pose
```

如果接收端配置了 `topic="pose"`，它会：

1. 只接收以 `pose` 开头的消息；
2. 从消息开头去掉这 4 个 ASCII 字节；
3. 把后面的内容当作 1280 字节 JSON header 开始解析。

因此发送端和接收端必须使用相同的 topic。topic 本身没有额外的换行符、空格或 `\0` 分隔符。

### 2. JSON header

header 是 UTF-8/ASCII 可解析的 JSON 文本，但在网络消息中必须占据固定的 1280 字节：

```text
实际 JSON 文本 + NUL 字节填充 = 1280 字节
```

header 至少需要描述：

| 字段 | 含义 |
|---|---|
| `v` | 协议版本，例如 `1` 或 `3` |
| `endian` | 二进制 payload 的字节序，通常为 `"le"` |
| `fields` | 二进制字段描述数组 |

每个 `fields` 元素包含：

| 字段 | 含义 | 示例 |
|---|---|---|
| `name` | 字段名 | `joint_pos` |
| `dtype` | 二进制数据类型 | `f64`、`f32`、`i64` |
| `shape` | 数组形状 | `[1, 29]` |
| `optional` | 是否可选，通常只在需要时提供 | `true` |

例如，Protocol v1 的单帧 header 可以写成：

```json
{
  "v": 1,
  "endian": "le",
  "fields": [
    {"name": "body_quat", "dtype": "f64", "shape": [1, 4]},
    {"name": "frame_index", "dtype": "i64", "shape": [1]},
    {"name": "joint_pos", "dtype": "f64", "shape": [1, 29]},
    {"name": "joint_vel", "dtype": "f64", "shape": [1, 29]}
  ]
}
```

这个 JSON 文本后面必须补 `\0`，直到 header 总长度达到 1280 字节。

### 3. 二进制 payload

payload 不再是 JSON，也不包含字段名。它只保存数值，并且严格按照 header 中 `fields` 的顺序连续拼接。

例如 header 顺序是：

```text
body_quat -> frame_index -> joint_pos -> joint_vel
```

那么 payload 顺序必须是：

```text
body_quat 的二进制字节
frame_index 的二进制字节
joint_pos 的二进制字节
joint_vel 的二进制字节
```

接收端根据每个字段的 `dtype` 和 `shape` 计算字节数，然后依次切片。发送端不能交换字段顺序。

## 二、数组形状和单帧发送

协议中的 `N` 表示一条消息携带的帧数，也就是 batch size。

如果每条消息发送一帧：

```text
N = 1
```

所以字段形状应为：

```text
joint_pos   [1, 29]
joint_vel   [1, 29]
body_quat   [1, 4]
frame_index [1]
```

如果一条消息发送 10 帧：

```text
joint_pos   [10, 29]
joint_vel   [10, 29]
body_quat   [10, 4]
frame_index [10]
```

当前项目的 Python 发布器采用 `N=1`，即 30 Hz 每次发送一帧。

## 三、Protocol v1

Protocol v1 是 **Joint-Based** 模式，对应 encode mode `0`。

### 必填字段

官方文档将以下两个字段列为 v1 的协议字段：

| 字段 | 形状 | 类型 | 含义 |
|---|---:|---|---|
| `joint_pos` | `[N, 29]` | `f32` / `f64` | 按 IsaacLab 顺序排列的 29 个 G1 关节位置 |
| `joint_vel` | `[N, 29]` | `f32` / `f64` | 按 IsaacLab 顺序排列的 29 个 G1 关节速度 |

此外，网页文档的 **Common Fields (All Versions)** 小节规定，所有版本还需要：

| 字段 | 形状 | 类型 | 含义 |
|---|---:|---|---|
| `body_quat` | `[N, 4]` 或 `[N, num_bodies, 4]` | `f32` / `f64` | 身体四元数，顺序为 `w, x, y, z` |
| `frame_index` | `[N]` | `i32` / `i64` | 单调递增的帧索引，用于帧对齐 |

因此，实际完整的 v1 消息至少应包含：

```text
body_quat
frame_index
joint_pos
joint_vel
```

### v1 单帧二进制大小示例

当前发布器使用：

```text
body_quat   f64[1, 4]  =  4 × 8  =  32 字节
frame_index i64[1]     =  1 × 8  =   8 字节
joint_pos   f64[1, 29] = 29 × 8  = 232 字节
joint_vel   f64[1, 29] = 29 × 8  = 232 字节
```

因此：

```text
binary payload = 32 + 8 + 232 + 232 = 504 字节
```

再加上 topic 和 header：

```text
完整 ZMQ 消息大小 = len("pose") + 1280 + 504 = 1788 字节
```

## 四、Protocol v3

Protocol v3 是 **Joint + SMPL Combined** 模式，对应 encode mode `2`。

### 必填字段

v3 必须同时提供关节数据和 SMPL 数据：

| 字段 | 形状 | 类型 | 含义 |
|---|---:|---|---|
| `joint_pos` | `[N, 29]` | `f32` / `f64` | IsaacLab 顺序的 G1 关节位置 |
| `joint_vel` | `[N, 29]` | `f32` / `f64` | IsaacLab 顺序的 G1 关节速度 |
| `smpl_joints` | `[N, 24, 3]` | `f32` / `f64` | 24 个 SMPL 关节的 xyz 位置 |
| `smpl_pose` | `[N, 21, 3]` | `f32` / `f64` | 21 个 SMPL 身体关节的轴角旋转 |

另外仍然需要所有版本共有的：

```text
body_quat
frame_index
```

所以 v3 的完整最小字段集合是：

```text
body_quat
frame_index
joint_pos
joint_vel
smpl_joints
smpl_pose
```

官方文档特别说明：在 v3 中，`smpl_joints` 和 `smpl_pose` 携带主要身体运动信息；`joint_pos` 中重点使用 6 个手腕关节，IsaacLab 顺序中的索引为：

```text
[23, 24, 25, 26, 27, 28]
```

其他 23 个关节可以为 0，非手腕关节的 `joint_vel` 也可以为 0。字段本身仍然必须存在，并且形状必须完整为 `[N, 29]`。

## 五、v1 和 v3 的区别

| 对比项 | Protocol v1 | Protocol v3 |
|---|---|---|
| 定位 | 纯关节数据 | 关节数据 + SMPL 数据 |
| encode mode | `0`，joint-based | `2`，SMPL-based |
| `joint_pos` | 必填，`[N, 29]` | 必填，`[N, 29]` |
| `joint_vel` | 必填，`[N, 29]` | 必填，`[N, 29]` |
| `smpl_joints` | 不需要 | 必填，`[N, 24, 3]` |
| `smpl_pose` | 不需要 | 必填，`[N, 21, 3]` |
| `body_quat` | 需要，公共字段 | 需要，公共字段 |
| `frame_index` | 需要，公共字段 | 需要，公共字段 |
| 主要运动来源 | G1 关节位置和速度 | SMPL 身体姿态，外加关节/手腕数据 |
| 适用场景 | 已经有 G1 qpos/qvel 的来源 | VR/PICO 等提供 SMPL 和手腕信息的全身遥操作 |

## 六、v1 和 v3 的封装是否相同？

**相同的是 ZMQ wire format，不同的是协议字段内容。**

两者都使用同样的外层封装：

```text
[topic_prefix]
[1280-byte JSON header]
[按 fields 顺序拼接的 binary payload]
```

两者都使用同样的 header 描述方式：

```json
{
  "v": 1,
  "endian": "le",
  "fields": [
    {"name": "...", "dtype": "f64", "shape": [...]}
  ]
}
```

区别只有：

1. header 中的 `v` 从 `1` 变为 `3`；
2. `fields` 中增加 v3 要求的 `smpl_joints` 和 `smpl_pose`；
3. payload 按新的 `fields` 顺序增加对应的二进制数据；
4. v3 的 `joint_pos` / `joint_vel` 仍然存在，但语义重点变为手腕关节控制。

### v1 header 示例

```json
{
  "v": 1,
  "endian": "le",
  "fields": [
    {"name": "body_quat", "dtype": "f64", "shape": [1, 4]},
    {"name": "frame_index", "dtype": "i64", "shape": [1]},
    {"name": "joint_pos", "dtype": "f64", "shape": [1, 29]},
    {"name": "joint_vel", "dtype": "f64", "shape": [1, 29]}
  ]
}
```

### v3 header 示例

```json
{
  "v": 3,
  "endian": "le",
  "fields": [
    {"name": "body_quat", "dtype": "f64", "shape": [1, 4]},
    {"name": "frame_index", "dtype": "i64", "shape": [1]},
    {"name": "joint_pos", "dtype": "f64", "shape": [1, 29]},
    {"name": "joint_vel", "dtype": "f64", "shape": [1, 29]},
    {"name": "smpl_joints", "dtype": "f64", "shape": [1, 24, 3]},
    {"name": "smpl_pose", "dtype": "f64", "shape": [1, 21, 3]}
  ]
}
```

### v3 单帧 payload 大小示例

如果所有字段使用 `f64`，则 v3 单帧 payload 为：

```text
body_quat   =  4 × 8       =   32 字节
frame_index =  1 × 8       =    8 字节
joint_pos   = 29 × 8       =  232 字节
joint_vel   = 29 × 8       =  232 字节
smpl_joints = 24 × 3 × 8   =  576 字节
smpl_pose   = 21 × 3 × 8   =  504 字节
--------------------------------
总 payload                  1584 字节
```

若 topic 仍为 `pose`，完整消息大小是：

```text
4 + 1280 + 1584 = 2868 字节
```

实际项目也可以使用 `f32`，此时数值精度和 payload 大小会相应变化，但 header 的 `dtype` 必须与二进制数据真实类型一致。

## 七、当前 Python 发布器的状态

当前项目中的 [`publish_g1_joint_data.py`](../publish_g1_joint_data.py) 实现的是 Protocol v1：

```text
topic       = pose
version     = 1
N           = 1
body_quat   = CSV 第 4~7 列
joint_pos   = Kimodo CSV 的 MuJoCo 顺序转换为 IsaacLab 顺序后的 29 个关节
joint_vel   = 按 source-rate 对转换后的 joint_pos 做中心差分
frame_index = 发布帧号
dtype       = f64 / i64
endian      = little-endian
```

它不能直接作为 v3 发送器，因为 v3 还缺少：

```text
smpl_joints
smpl_pose
```

如果数据源只有当前这个 36 列 G1 CSV，而没有 SMPL 关节位置和 SMPL 轴角姿态，应该使用 v1。不能为了改成 v3 而把缺失的 SMPL 数据填成没有实际意义的零值。

## 八、注意事项

### 1. `body_quat` 不是根部位置

当前 CSV 的前 7 列可解释为：

```text
第 0~2 列：根部位置
第 3~6 列：根部四元数
```

Protocol v1/v3 的公共字段需要的是 `body_quat`。根部位置没有被当前 v1 发布器发送。

### 2. 关节顺序必须是 IsaacLab 顺序

协议只接受 29 个数还不够，顺序也必须是 GR00T/SONIC 使用的 IsaacLab G1 joint order。若 CSV 的列顺序是其他模型、URDF 或 Unitree SDK 顺序，需要先做关节映射。

本项目使用的 Kimodo G1 CSV 是 MuJoCo qpos 顺序。发布器默认使用官方的 `mujoco_to_isaaclab` 映射后再发送；如果输入文件本身已经是 IsaacLab 顺序，可运行时指定 `--input-order isaaclab`。

### 3. 协议版本不能在一次会话中切换

同一次 ZMQ streaming session 中，不能先发送 `v=1`，之后再发送 `v=3`。如果确实要切换，应停止当前会话并重新建立流。

### 4. `f32` 和 `f64` 必须匹配

如果 header 写：

```json
{"dtype": "f32"}
```

payload 就必须使用 32 位浮点数；如果写 `f64`，payload 就必须使用 64 位浮点数。接收端通过 header 计算字节数，不会根据 JSON 数字文本自动推断类型。

### 5. topic、端口和 host 不是协议版本

下面这些是连接配置，不是 v1/v3 的字段：

```text
host
port
topic
```

v1/v3 的版本由 header 中的：

```json
"v": 1
```

或：

```json
"v": 3
```

决定。
