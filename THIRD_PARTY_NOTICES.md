# 第三方来源与版本记录

本项目是 Kimodo、SONIC 与 MCP 的集成工程。以下组件独立获取，不随本项目的源码包分发。本文记录 2026-10-13 从现有服务器读取的版本，不代表各上游的最新版本。

| 组件 | 上游与验证版本 | 作用与许可来源 |
| --- | --- | --- |
| Kimodo | [nv-tlabs/kimodo](https://github.com/nv-tlabs/kimodo)，提交 `1aece8c124d73d255ceff5086d983b844c9f4e94` | 动作生成与 AMASS 导出。源码 [Apache-2.0](https://github.com/nv-tlabs/kimodo/blob/1aece8c124d73d255ceff5086d983b844c9f4e94/LICENSE)；模型及其依赖按各自发布条款获取。 |
| GR00T-WholeBodyControl / SONIC | [NVlabs/GR00T-WholeBodyControl](https://github.com/NVlabs/GR00T-WholeBodyControl)，IOENV 容器内提交 `0a87181c9106d0e49293400714b157676e0ec664` | 全身控制、部署与仿真。该版本 [LICENSE](https://github.com/NVlabs/GR00T-WholeBodyControl/blob/0a87181c9106d0e49293400714b157676e0ec664/LICENSE) 区分 Apache-2.0 源码与 NVIDIA Open Model License 模型权重。 |
| IOENV CLI | [ioai-tech/ioenv_cli](https://github.com/ioai-tech/ioenv_cli)，提交 `ec1f8354095de91a234dc31a9213ee1a410b5cc9` | Docker 环境管理、GPU 与显示环境配置。CLI 使用 [MIT License](https://github.com/ioai-tech/ioenv_cli/blob/ec1f8354095de91a234dc31a9213ee1a410b5cc9/LICENSE)。 |
| IOENV SONIC 仿真镜像 | `ioaitech/sonic_sim:1.0`，镜像 ID `sha256:b22ae068640f0a699eb1478bd5d1b28af9fa9d369de7dbe0905cf77121e3cfdd` | 提供当前使用的 SONIC 环境；镜像内容的组件许可分别适用，CLI 的 MIT 许可不覆盖镜像中全部组件。 |

SONIC 控制器由 NVIDIA 提供，IOENV 由 IO-AI TECH 提供。本项目没有训练或开发上述基础模型，也不将它们描述为本项目原创成果。

## 本项目的集成工作

- `kimodo_sonic_service/`：Kimodo 调用封装、SMPL-X 导出适配、任务元数据、MCP 工具、生成锁与推流进程管理。
- `publish_g1_smpl_data.py` / `publish_g1_joint_data.py`：针对 SONIC 接口的 Protocol v3 / v1 发布工具；协议依据 [SONIC 文档](https://nvlabs.github.io/GR00T-WholeBodyControl/tutorials/zmq.html) 与接收端代码。v3 发布器内置的 SMPL 静态骨架与转换常量依据 SONIC 的人体骨架定义，保留 NVIDIA 来源声明与对应许可。
- 命令行客户端、测试、部署配置与集成说明。

这些集成文件单独整理用于项目展示。集成代码尚未指定整体开源许可证；仓库展示不改变第三方许可，也不自动授予集成代码的开源使用许可。正式开源时由维护者选择许可，并保留本页来源声明。

## 获取入口

- [Kimodo 官方项目与安装说明](https://github.com/nv-tlabs/kimodo)
- [NVIDIA SONIC 官方项目](https://github.com/NVlabs/GR00T-WholeBodyControl)
- [IOENV CLI](https://github.com/ioai-tech/ioenv_cli)
- [IO-Creed SONIC 仿真与部署教程](https://io-ai.tech/iocreed/docs/locomotion_01/)

模型权重、SMPL / SMPL-X 资源、Hugging Face 缓存与凭据均需自行准备，不包含在发布包中。

当前验证版本的许可原文保存在 [licenses/](licenses/README.md)，仅用于保留第三方许可记录；它们不是本项目整体许可证。
