# 39 · 完整代码审查：RAG、安全与居务闭环

> 日期：2026-11-12  
> 范围：全仓静态审计、36 套 2060 项回归、RAG/权限/审计人工复核、居委会业务缺口对账。  
> 原则：文档声称的能力必须能由代码、数据库约束和独立测试复核。

## 1. 审查结论

既有系统已具备 FDE、物业工单、台账、收发文、周报、居民端、RAG、RBAC/ABAC、
证据链和审计等较完整基础，但本轮发现四个会影响“可审核”结论的真实问题：

1. Word 导出与诉求聚类已在功能清单中宣称完成，但根依赖清单缺 `python-docx`、`numpy`；
2. FDE 的咨询类型和结构化敏感字段完全依赖模型输出，模型漂移会让 RAG 不触发、字段密级为空；
3. RAG 未把知识文档视为不可信输入，缺提示词注入隔离、引用哈希和生成相关性门槛；
4. `audit_log` 文档标称“只追加”，数据库实际上允许 UPDATE/DELETE；同时最“居委会”的会议议事模块仍为空白。

以上均已修复并增加回归。

## 2. RAG 加固

- 知识文档入库拒绝疑似模型控制指令；历史库或离线同步带入的脏切片在生成前隔离；
- 身份证号、个人手机号不得进入知识库，要求先脱敏；
- 问题最多 500 字、检索词最多 300 字、单文档最多 20 万字；
- 生成阶段仅采用归一化分不低于 0.15、查询覆盖率不低于 0.08 的片段；
- 恶意首条被隔离后，低相关候选不得“凑答案”，无依据时明确转人工；
- 每条引用返回 `doc_id/source/heading/score/doc_hash/chunk_hash`；
- 模型回答未引用本次命中文件时，自动降级为带出处的原文摘录；
- 查询与问答审计记录真实工作人员，不再统一记为 `system`。

验证：`scripts/smoke_kb.py` 33/33，通过检索质量、模型不可用降级、权限、提示词注入、
强敏感信息拒绝、历史脏文档隔离、真实操作者审计和 FDE 咨询联动。

## 3. 首录与保密加固

模型输出之后增加确定性校验，只处理原文可直接复核的硬事实：

- “来问/怎么申请/需要哪些材料”等显式意图优先判为咨询；
- 唯一出现的楼栋、单元、房号、姓名和已脱敏电话用于补全结构化字段；
- 电话补全使用 `138****1234` 形式，明文仍不进入模型和数据库；
- 校正原因写入 Agent 协作轨迹，可回放“模型输出 + 规则校正”。

验证：`scripts/smoke_tasks.py` 32/32，电话 3 级、姓名/房号 2 级、楼栋 1 级均落库；
`scripts/smoke_kb.py` 验证高龄津贴咨询稳定触发知识检索。

## 4. 审计账本加固

- ORM `before_update/before_delete` 阻止误改；
- SQLite 启动迁移创建 `BEFORE UPDATE/DELETE` 触发器，直接 SQL 同样被拒绝；
- 游标分页和查询码锁定复合索引保留；
- 口径明确为“应用层与数据库约束下只追加”，不宣称拥有数据库管理员权限的人绝对无法篡改；
- 工单/居民诉求争议证据仍使用独立 SHA-256 证据链与链头锚定。

验证：`scripts/smoke_security_hard.py` 71/71，包含直接 SQL 更新/删除拒绝断言。

## 5. 居委会议事闭环

新增 `/meetings` 与 `/api/meetings`：

1. 社区建会并录入议题；街道只读监督；物业无权接触内部会议材料；会议必须归属已有在职账号的真实社区；
2. 发出通知后才能登记签到和定稿；
3. 居民代表会议必须“实到人数严格超过应到一半”；
4. 每项决议可指定社区/物业承办单位与期限，定稿时自动生成 `Task`；
5. `task_id` 回写议题，重复定稿不会重复派单；
6. 内部纪要 `minutes_md` 与公开摘要 `public_summary` 分列；
7. 只有 `is_public=true` 的决议进入居务公开，发布前再次拦截身份证、个人手机号、居民姓名和精确门牌；
8. 居务公开生成已发布 `Notice`，居民端可见；重复发布幂等；
9. 创建、加议题、通知、决议、定稿、发布六类关键动作全部写审计。

验证：`scripts/smoke_meetings.py` 35/35。

## 6. 依赖与可迁移性

根 `requirements.txt` 与发布镜像清单统一增加：

- `python-docx>=1.1`：收文 `.docx` 解析、红头 Word 与水印导出；
- `numpy>=1.26`：居民诉求 TF-IDF + 层次聚类。

这两项均有 Windows/Linux 预编译 wheel。`openpyxl` 仍不是运行时依赖，Excel 导出继续使用项目内
`app/xlsx.py`。

## 7. 剩余边界

- 公网部署仍必须启用 HTTPS 并设置 `SESSION_COOKIE_SECURE=1`；
- 演示账号是公开口令，公网实例必须执行口令轮换；
- MySQL 仍未跑完整回归，当前数据库只追加触发器只在 SQLite 自动建立；
- 限流与模型准入是单进程内存实现，多 worker 部署需要 Redis；
- RAG 当前默认是字符二元组 BM25，真实社区文件替换后仍需重跑 Top1/Top3 与同义改写评测；
- 会议模块当前记录人数，不记录实名签到表；如进入真实生产，实名签到应作为受控附件或独立敏感表，
  不能直接塞进公开摘要。

## 8. 验证命令

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\audit_all.py --offline
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_kb.py
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_tasks.py
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_security_hard.py
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_meetings.py
.\.venv\Scripts\python.exe -X utf8 scripts\smoke_feedback_loop.py
```

最终结果：36 套回归合计 2060 项，失败 0 项。居民数据治理与纠错闭环的详细边界见
`docs/40-居民数据治理与模型纠错闭环.md`。
