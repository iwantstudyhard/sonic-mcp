# MCP 使用说明

MCP 服务使用 Streamable HTTP，路径为 `/mcp`。客户端连接时由 SDK 完成协议级 initialize；下表的 `initialize` 是便于人工检查的工具。

| 工具 | 作用 |
| --- | --- |
| `initialize` | 返回默认参数、鉴权状态、能力和服务健康信息 |
| `service_health` | 返回包可用性、生成锁和推流进程状态 |
| `kimodo_generate` | 生成动作并导出 NPZ / CSV；完成后返回 `job_id` |
| `job_status` | 查询一个任务 |
| `list_jobs` | 按创建时间倒序列出任务 |
| `sonic_stream_start` | 用已完成的任务启动 v3 推流 |
| `sonic_stream_stop` | 停止当前推流 |
| `workflow_generate_and_stream` | 生成、导出并启动推流；调用前接收端应已准备好 |

## 推荐调用

先检查、生成，再确认 SONIC 接收端准备好后推流：

```text
initialize → kimodo_generate → sonic_stream_start → sonic_stream_stop
```

也可以使用 `workflow_generate_and_stream` 一次执行生成和推流。

生成参数示例：

```json
{
  "prompt": "a person walks and waves",
  "duration_seconds": 5,
  "denoising_steps": 100,
  "seed": 0
}
```

推流参数示例：

```json
{
  "job_id": "<实际job_id>",
  "loop": false,
  "endpoint": "tcp://*:5556",
  "topic": "pose",
  "rate": 50,
  "wrist_source": "smpl"
}
```

`endpoint` 是发布器的本机绑定地址，工具只允许 `*`、`127.0.0.1`、`localhost` 的 TCP 绑定，不接受任意命令或远端地址。

## 状态与超时

- `generating`：正在生成，或服务异常中断后遗留的记录；结合当前锁和日志判断。
- `ready`：生成完成，存在完整 NPZ 与 CSV。
- `failed`：捕获了生成错误，摘要在 `job.json`，完整堆栈在服务日志。

生成工具将耗时操作移到工作线程，调用本身仍等待生成完成，**不会立即返回 job_id**。首次模型加载可能超过 60 秒，客户端可将工具超时调整为 300 秒；客户端超时或取消不保证终止工作线程。通过另一连接查询 `list_jobs` / `job_status`，避免重复提交。

`service_health` 中 `kimodo_available` 只检查 Python 包能否找到，`generation_running` 表示生成锁是否占用；文本编码器网页返回 HTTP 200 也不能证明一次编码请求一定成功。

## 可选鉴权

默认不设置 Token。需要鉴权时设置 `KIMODO_MCP_TOKEN` 和客户端可达的 `KIMODO_MCP_PUBLIC_URL`，客户端使用同一个 Token；真实值保存在 `.env`，发布模板只包含空值。

客户端示例：

```bash
export KIMODO_MCP_TOKEN='<至少24个字符的实际密钥>'
python3 call_kimodo_sonic_mcp.py --url http://<服务器IP>:8000/mcp health
```
