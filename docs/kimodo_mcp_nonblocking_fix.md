# Kimodo MCP 单 GPU 与非阻塞生成修正

## 修正内容

- 仅 text-encoder 使用 GPU 0：设置 `NVIDIA_VISIBLE_DEVICES=0`、
  `CUDA_VISIBLE_DEVICES=0`，GPU reservation 使用 `device_ids: ["0"]`。
  Kimodo 动作模型的 GPU 配置保持不变。
- text-encoder 和 kimodo-mcp 的 CPU 范围设置为 `1-111`，避开 SONIC 的
  CPU 0 实时线程。此服务器有 112 个逻辑 CPU；在不同机器上须通过
  `KIMODO_CPUSET` 设置该机器有效的范围（例如 16 个逻辑 CPU 时设置为 `1-15`）。
  实测排除 CPU 0 后，卡在 MKL 初始化的进程立即恢复模型加载。
- 该版本的 LLM2Vec 看到多张 GPU 会创建 spawn 子进程；本机子进程卡在
  PyTorch/MKL 初始化。单 GPU 避开这一分支，不修改 NVIDIA 的生成算法。
- `kimodo_generate` 与 `workflow_generate_and_stream` 使用工作线程执行耗时操作。
  生成期间，MCP 初始化、健康检查、任务查询仍可响应。
- 健康检查只探测 Kimodo 包是否存在，不导入模型。`generation_running` 表示
  当前是否有生成任务占用生成锁；它不是文本编码服务的推理健康检查。
- `job.json` 原子替换，避免并发查询读到不完整内容；失败时容器日志打印完整堆栈。

## 旧服务器布局中的应用方式

以下记录对应 Kimodo 根目录加 `mcp/` 子目录的旧部署。独立项目使用 [部署说明](deployment.md) 的 `docker/compose.yaml`。

Compose 文件应位于 Kimodo 根目录，MCP 项目位于其 `mcp/` 子目录。
源码通过 bind mount 生效，不需要为本次 Python 修改重新构建镜像。

```bash
cd <Kimodo源码目录>
docker compose up -d --no-deps text-encoder
docker compose ps text-encoder
# 等 text-encoder 显示 healthy 后，用 up 应用 MCP 的新 CPU 配置。
docker compose up -d kimodo-mcp
docker compose logs -f --timestamps --tail 100 kimodo-mcp text-encoder
```

修改 Compose 环境变量后必须使用 `up` 重新创建容器，单独 `restart` 不会应用新变量。
重启会中断正在执行的旧任务；旧任务的 `generating` 记录不会自动恢复执行。

## 验证方法

连接 `http://<服务器IP>:8000/mcp`，调用 `kimodo_generate`，先用
`duration_seconds=1`、`denoising_steps=10`，再用正常的 5 秒、100 步参数。
在生成尚未完成时，从第二个 MCP 连接调用 `initialize`、`service_health`、
`list_jobs` 和 `job_status`，它们应立即响应。最终任务应为 `ready`，
目录内应同时存在 `motion.npz`、`motion.csv` 和 `job.json`。

两个生成工具仍然等待动作生成完成后返回原有结果，**不是立即返回 job_id 的任务队列**。
如果首次模型加载超出客户端 60 秒限制，需调大工具调用超时；超时后不要重复提交，
先用另一连接查询任务状态。请求取消不保证终止已经运行的工作线程。
同一时刻只允许一个生成任务，新增请求会明确返回“已有 Kimodo 生成任务正在运行”。

自动检查并发响应与真实生成（不会启动 SONIC 推流）：

```bash
python3 tests/check_remote_generation.py \
  --url http://127.0.0.1:8000/mcp --duration 5 --steps 100
```

结果应包含 `status=ready`、`responsive_during_generation=true`，并列出
生成过程中协议初始化、工具列表、健康检查和任务查询的响应耗时。

实现依据：[Python 工作线程接口](https://docs.python.org/3.10/library/asyncio-task.html#asyncio.to_thread)。

## 2026-10-13 服务器实测结果

在 `<服务器IP>` 上验证，本地和容器内的 12 项回归测试全部通过。
text-encoder 只看到 1 张 GPU，生成后没有 spawn 编码子进程残留。

| 输入 | 任务 ID | 完整调用耗时 | 生成期间控制请求最大耗时 | 结果 |
| --- | --- | --- | --- | --- |
| 1 秒、10 步，首次加载动作模型 | `20261013-033103-7de42fe5` | 10.213 秒 | 532.66 毫秒 | ready |
| 5 秒、100 步，`a man dance` | `20261013-033152-795200c8` | 2.075 秒 | 19.60 毫秒 | ready |

控制请求包含协议初始化、工具列表、initialize 工具、健康检查、任务列表和单任务查询。
舞蹈任务的 CSV 形状为 `[150,36]`，NPZ 局部旋转为 `[150,22,3,3]`，
数据均为有限数，源帧率为 30 Hz。CSV 仍是同步 companion CSV，未改变为完整 G1 重定向。
测试仅生成文件，没有启动新的 SONIC 推流。

部署前的旧备份已在目录整理时迁入仓库外的私有归档，具体位置见服务器整理报告。
