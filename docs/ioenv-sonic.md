# SONIC 与 IOENV 接入

本项目的 SONIC 仿真环境来自 **[IO-AI TECH 的 IOENV CLI](https://github.com/ioai-tech/ioenv_cli)**，当前使用 `ioaitech/sonic_sim:1.0` 镜像、`sonic_sim_env` 容器。容器内的 SONIC 控制器来自 **[NVIDIA GR00T-WholeBodyControl](https://github.com/NVlabs/GR00T-WholeBodyControl)**。

IOENV 负责环境启动，SONIC 负责控制；本项目通过 MCP 调用 Kimodo，再向 SONIC 提供动作数据。此处 IOENV 指 `ioai-tech/ioenv_cli`，安装步骤以 [IO-Creed 官方教程](https://io-ai.tech/iocreed/docs/locomotion_01/) 为依据。

## 获取环境

```bash
git clone https://github.com/ioai-tech/ioenv_cli.git
cd ioenv_cli
source ioenv.sh
ioenv pull sonic_sim
# 国内镜像下载入口：ioenv pull sonic_sim --from-mirror
ioenv run sonic_sim
```

复现现有部署时，可对照 [版本记录](../THIRD_PARTY_NOTICES.md) 检查 CLI 和容器内 SONIC 提交。容器镜像标签可能更新，版本记录包含当前镜像 ID；当前本地镜像没有可记录的 RepoDigest。

## 启动仿真与控制程序

在第一个容器终端启动仿真：

```bash
cd /opt/GR00T-WholeBodyControl
source .venv_teleop/bin/activate
python gear_sonic/scripts/run_sim_loop.py
```

在第二个终端进入同一环境，启动 ZMQ 控制：

```bash
ioenv run sonic_sim
cd /opt/GR00T-WholeBodyControl/gear_sonic_deploy
bash deploy.sh --input-type zmq --zmq-host <Kimodo宿主机可达IP> sim
```

请将占位符替换为实际 IP。按该版本控制程序的交互提示切换到 ZMQ streaming 模式；已验证版本通过 `]` 后按 `Enter` 切换，应显示 `ZMQ STREAMING MODE: ENABLED`。[SONIC ZMQ 操作说明](https://nvlabs.github.io/GR00T-WholeBodyControl/tutorials/zmq.html)

本项目直接使用 ZMQ motion stream，因此使用 `--input-type zmq`。IOENV 官方教程中的 PICO 管理器流程使用 `zmq_manager`，它对应另一种输入方式。

## 推流

先生成动作取得 `job_id`，再启动推流：

```bash
python3 call_kimodo_sonic_mcp.py --url http://<服务器IP>:8000/mcp \
  stream-start <job_id> --loop
```

在调试过程中循环播放便于观察；停止时执行：

```bash
python3 call_kimodo_sonic_mcp.py --url http://<服务器IP>:8000/mcp stream-stop
```

发布器默认绑定 `tcp://*:5556`，不是订阅端要连接的地址。订阅端使用 `tcp://<Kimodo宿主机可达IP>:5556`，topic 为 `pose`。

## 当前验证环境的网络特征

`sonic_sim_env` 使用 Docker `host` 网络，MCP 使用独立 bridge 网络并将 5556 映射到宿主机。在当前服务器上，Docker 对回环地址没有提供有效的 5556 转发，`127.0.0.1:5556` 会连接失败；宿主机的可达 LAN / Tailscale IP 已验证可用。

这个结果由本机 Docker 转发配置决定，并非所有 Docker `host` 网络都无法使用 localhost。新部署应实际验证连接。不要仅依据容器进程仍存在就判断动作已经送达：发布器会等待接收端握手。

IOENV 容器中的 `/root/workspace` 来自宿主机 `~/ioenv_ws/sonic_sim_ws`。SONIC 源码位于容器的 `/opt/GR00T-WholeBodyControl`，不在本项目发布目录内；模型、Docker 数据卷和工作区均单独保留。
