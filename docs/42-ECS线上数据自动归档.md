# ECS 线上数据自动归档

## 目标与边界

居委会试用期间，ECS 是业务写入端，本机只接收备份。同步任务通过 SSH 在服务器上调用 `snapshot_apply.py --export`，使用 SQLite 在线备份 API 生成一致快照，再经 SCP 下载到本机归档目录。

自动任务**不会**调用 `ecs_sync.py pull`，不会覆盖本机 `data/community.db`，不会把本机数据推回服务器，也不会修改服务器业务库。归档可用于后续抽样、复盘和论文统计；需要恢复到本机工作库时，必须先手动复制并确认目标库。

## 安装与检查

```powershell
.venv\Scripts\python.exe -X utf8 scripts\ecs_archive_sync.py --archive-dir F:\bese4-1 --install-task
.venv\Scripts\python.exe -X utf8 scripts\ecs_archive_sync.py --archive-dir F:\bese4-1 --status
```

Windows 任务计划程序创建当前用户任务 `CommunityAgent-ECS-Archive`：登录后立即同步，随后每小时同步一次。电脑关机或休眠期间无法实时拉取；恢复运行后计划任务会补跑。也可手动执行脚本立即拉取。

本机试用归档目录：`F:\bese4-1`。也可以通过 `--archive-dir` 或 `COMMUNITY_ARCHIVE_DIR` 指定其他目录。目录 ACL 限制为当前用户、SYSTEM 与本机管理员；默认保留 180 天。相同 SHA-256 的快照只保存一次，避免无业务变化时重复占用空间。`manifest.jsonl` 记录归档时间、校验值、文件大小和各表行数，不记录业务正文。

## 校验与恢复

下载后必须通过 SQLite `PRAGMA quick_check`、关键表存在性检查和 SHA-256 计算才会原子落档。失败或断网时保留已有归档，不生成正式快照。每个 `.db` 文件本身包含完整业务记录，可能含姓名、电话、地址、诉求和审计信息；不要将归档目录同步到网盘、公开仓库或共享电脑。

查看任务状态：

```powershell
Get-ScheduledTask -TaskName CommunityAgent-ECS-Archive | Get-ScheduledTaskInfo
```

查看归档文件时应使用副本或只读连接，不要直接替换项目 `data/community.db`。线上数据库继续由社区人员在 ECS 正常写入。
