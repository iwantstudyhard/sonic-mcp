# 故障排查

独立部署在项目根目录查看日志：

```bash
docker compose --env-file .env -f docker/compose.yaml logs \
  -f --timestamps --tail 100 kimodo-mcp text-encoder
```

旧服务器部署在原 Kimodo 根目录执行 `docker compose logs -f --timestamps --tail 100 kimodo-mcp text-encoder`。任务错误另见输出目录中的 `job.json`。

| 现象 | 已知原因 / 检查方法 |
| --- | --- |
| `Connection refused`，MCP 日志停在编码调用 | 检查 text-encoder 是否完成模型加载；容器 `Up` 不等于编码已就绪 |
| `Temporary failure in name resolution` | 检查两个服务是否处于同一 Compose 网络，编码器服务名是否为 `text-encoder` |
| 文本编码卡在 PyTorch / MKL 初始化 | 当前验证服务器的多 GPU 初始化曾卡住；编码器仅分配一张 GPU，并按实际核心数设置 CPU 范围 |
| 生成期间其他 MCP 调用超时 | 确认运行的是使用 `asyncio.to_thread` 的版本，另检查客户端工具超时 |
| `local_rot_mats should be 5D` | 确认 `backend.py` 使用批量 prompt，并在 AMASS 导出前统一 batch 维度 |
| 推流进程 `running=true` 但仿真无动作 | 检查订阅 IP、5556 端口、`pose` topic 和 streaming 模式；等待握手时进程也会保持运行 |
| `当前 Python 环境未安装 kimodo` | 检查基础镜像和挂载路径；不要用只有 MCP 文件的目录覆盖整个 `/workspace`，从而遮住 `/workspace/kimodo` |
| 修改配置后无变化 | 更改 Compose 环境变量须使用 `up` 重新创建；镜像内源码更新须重新构建；旧 bind mount 部署则须重启进程 |

当前 v3 生成的 CSV 的 29 个机器人关节是占位零值。用它喂给 v1 关节发布器会得到错误用途的输入，应使用完整 G1 重定向 CSV。

## 验证工具

不需要 GPU 的回归测试：

```bash
python3 -m unittest discover -s tests -v
```

需要在线 Kimodo 的可选生成检查：

```bash
python3 tests/check_remote_generation.py \
  --url http://<服务器IP>:8000/mcp --duration 5 --steps 100
```

这个检查会生成一个新动作并查询并发响应，不启动 SONIC 推流。它目前用于不配置 Token 的 JSON 响应服务；启用鉴权时请通过正常 MCP 客户端验证。
