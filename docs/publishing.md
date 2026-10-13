# 打包与 GitHub 展示

发布目录只包含集成源码、测试、文档和配置模板。Kimodo、SONIC 和 IOENV 通过来源链接和版本记录说明，按各自安装步骤另行获取。

## 打包

```bash
python3 scripts/package_release.py --output dist/sonic-mcp.zip
```

脚本按文件白名单建立 ZIP，附带 SHA-256 校验文件。它不会因新增运行目录而把模型或任务文件收进压缩包。ZIP 内统一使用 `sonic-mcp/` 顶层目录。

排除内容：真实 `.env`、SSH / Hugging Face 凭据、模型权重、Docker 数据卷、`kimodo_outputs`、`runtime` 中的任务、Python 缓存、旧部署备份和服务器私有报告。

## GitHub 展示

项目仓库：[iwantstudyhard/sonic-mcp](https://github.com/iwantstudyhard/sonic-mcp)。项目介绍：

> MCP integration for Kimodo text-to-motion generation and SONIC ZMQ streaming, with the SONIC simulation environment provided by IOENV.

本项目使用独立 Git 仓库，远端为 `https://github.com/iwantstudyhard/sonic-mcp.git`。原 Kimodo 仓库的 `origin` 属于上游，保持原有配置。

README 说明了第三方分工和当前能力边界。若添加演示视频，应配上运行环境与版本，并说明演示来自仿真还是实机。

## 许可状态

第三方来源见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。集成代码的整体开源许可尚未选择；作为项目展示和正式开放使用许可是不同的发布选择。正式开源时由维护者选定许可后添加根目录 `LICENSE`，不要直接将上游许可证套用于整个项目。
