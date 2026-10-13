# 部署与配置

## 两种目录布局

现有服务采用 Kimodo 上游仓库加 `mcp/` 子目录的布局：Compose 在 Kimodo 根目录，Kimodo 源码映射到 `/workspace/kimodo`，MCP 映射到 `/workspace/mcp`。

整理后的展示项目独立存放，本仓库的 `docker/compose.yaml` 用于新的独立部署：Kimodo 源码保留在仓库外，通过 `KIMODO_SOURCE` 构建基础镜像；MCP 源码由 `docker/Dockerfile.mcp` 复制到 `/workspace/mcp`，生成结果挂载到 `runtime/kimodo_outputs`。不需要复制整个 Kimodo 或 IOENV 仓库。

本次整理保留了原在线部署。独立配置已做配置解析与路径检查，尚未切换在线服务，也未重建 GPU 镜像。不要在相同端口上同时启动两套服务。

## 外部依赖

1. 按 [Kimodo 安装说明](https://github.com/nv-tlabs/kimodo) 准备源码、所需依赖和模型下载权限；包含 `MotionCorrection`、`kimodo-viser` 等其 Dockerfile 需要的目录。
2. 准备 Docker、Compose、NVIDIA Container Toolkit 与 Hugging Face 缓存。需要访问受限模型时，在宿主机完成登录，凭据留在本机缓存。
3. SONIC 环境按 [IOENV 接入说明](ioenv-sonic.md) 获取。

当前验证版本见 [第三方记录](../THIRD_PARTY_NOTICES.md)。复现旧版本时使用记录的提交号，并按该版本 README 准备依赖；上游更新后的行为可能不同。

## 独立 Docker 部署

在项目根目录操作：

```bash
cp .env.example .env
# 编辑 .env，KIMODO_SOURCE 必须指向本机外部 Kimodo 仓库的绝对路径。
mkdir -p runtime/kimodo_outputs
docker compose --env-file .env -f docker/compose.yaml config --quiet
docker compose --env-file .env -f docker/compose.yaml build text-encoder
docker compose --env-file .env -f docker/compose.yaml build kimodo-mcp
docker compose --env-file .env -f docker/compose.yaml up -d
docker compose --env-file .env -f docker/compose.yaml ps
```

两个构建按顺序执行：MCP 镜像依赖先生成的 Kimodo 基础镜像。Kimodo 依赖较多，模型下载不等于镜像构建，首次生成还可能加载模型。

MCP 源码放在镜像内；修改源码后需要重新构建 MCP 镜像。若沿用旧服务器的源码 bind mount 布局，则 Python 文件修改后重启进程即可；两种部署方式不要混用。

## 参数

| 配置 | 默认值 / 要求 |
| --- | --- |
| `KIMODO_SOURCE` | 外部 Kimodo 绝对路径，必填 |
| `KIMODO_CPUSET` | 留空使用可用 CPU；与 SONIC 同机时按实际 CPU 数排除实时线程核心 |
| `TEXT_ENCODER_GPU` | `0`，文本编码器只分配一张 GPU |
| `KIMODO_MCP_PORT` | 宿主机 HTTP 端口 `8000`；容器监听 `8000` |
| `TEXT_ENCODER_PORT` | 宿主机文本编码器端口 `9550` |
| `SONIC_ZMQ_PORT` | 宿主机 ZMQ 端口 `5556`；发布器仍绑定容器 `5556` |
| `KIMODO_MCP_TOKEN` | 可选 Bearer Token，至少 24 个字符 |
| `KIMODO_MCP_PUBLIC_URL` | 客户端可访问的服务根地址，启用 Token 时须设置正确 |
| `KIMODO_MODEL` | `Kimodo-SMPLX-RP-v1` |
| `KIMODO_SOURCE_FPS` | `30` |
| `KIMODO_MAX_DURATION` | `30` 秒 |
| `KIMODO_MAX_DENOISING_STEPS` | `200` |
| `HF_CACHE_DIR` | 宿主机 Hugging Face 缓存目录 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 留空；需要时设置容器能访问的代理地址 |

在已验证的 112 个逻辑 CPU 服务器上，`KIMODO_CPUSET=1-111` 避开 CPU 0 上的 SONIC 实时线程。在其他机器上必须调整；例如 16 个逻辑 CPU 可以使用 `1-15`。硬编码 `1-111` 不适用于所有机器。

更改宿主机 ZMQ 端口后，订阅端须使用相应端口；当前验证的 `deploy.sh` 只转发 `--zmq-host`，没有解析 `--zmq-port` / `--zmq-topic`，默认 5556 与 `pose` 最便于复现。

Compose 中通过 `http://text-encoder:9550/` 访问文本编码器，`NO_PROXY` 包含该服务名。不要把容器内的文本编码地址配置成宿主机的 `localhost`。

## 原生 Python 环境

先在当前 Python 环境按 Kimodo 文档安装 GPU、模型与编码依赖，再安装本项目依赖：

```bash
python3 -m pip install -r requirements-validated.txt
export TEXT_ENCODER_URL=http://127.0.0.1:9550/
python3 -m kimodo.scripts.run_text_encoder_server
```

另一终端在项目根目录执行：

```bash
python3 run_kimodo_sonic_mcp.py
```

服务入口读取环境变量，不会自动加载 `.env`。需要时在 Bash 中先执行 `set -a; source .env; set +a`。原生模式的 `KIMODO_OUTPUT_ROOT` 默认是项目根目录的 `kimodo_outputs/`；可设为 `runtime/kimodo_outputs`。

## 在线服务迁移

当前在线服务继续使用原路径和任务目录。迁移到独立项目时，应保留原输出数据、安排停止旧服务和释放端口，再启动独立配置并验证模型加载及 ZMQ 接收。服务器私有整理报告记录了原路径、归档位置和源文件校验值。
