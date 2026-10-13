# 本机运行数据

独立 Compose 部署将任务文件写入 `runtime/kimodo_outputs/<job_id>/`：
`job.json`、`motion.npz`、`motion.csv`。

这些文件由程序生成，`.gitignore` 和发布打包白名单会将其排除。
现有服务器部署仍使用原来的输出目录；目录整理不搬移在线任务数据。
