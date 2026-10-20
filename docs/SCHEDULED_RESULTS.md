# ChatGPT / Codex 定时任务结果采集

## 范围与当前状态

- 接收**执行结果**，不创建、修改、触发或停用原有定时任务。
- **Codex 本机：已实现真实只读扫描。**读取 `CODEX_HOME`（默认 `~/.codex`）下的 `sqlite/codex-dev.db`、`state_5.sqlite`，仅追踪已注册 cron 运行与 heartbeat 目标会话，在对应会话 JSONL 中按 `task_started` 的定时触发归属收集完成输出。不会把同一会话中的普通人工对话混进去。
- 每次重新核对全部本机可读的已注册定时运行，按稳定身份只保存新增/变化。因此也能发现旧日期补同步的结果，不靠消息时间跳过记录。
- **ChatGPT 网页 / 客户端：自动读取未连接。**只接入本地标准结果收件箱和 JSON 导入接口，没有调用未公开的云端接口，也不读取或拷贝登录凭据。2026-10-20 的真实网页验证停留在加载页，未读到任务列表；不能宣称“云端已全量扫描”。
- [OpenAI 官方文档](https://learn.chatgpt.com/docs/automations)说明可以在 Scheduled 查看任务和近期运行，并区分独立任务与对话内任务；该页面没有建立可供本程序直接读取完整结果的公开 API 合约。

## 使用

面板左栏 → **定时任务结果** → **扫描全部定时结果**。界面分别显示 Codex 读取情况与 ChatGPT 尚待连接状态。导入不调用模型、不创建或完成待办、不发邮件。

命令行（在 `W:\message-task-agent`）：

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m taskagent scan-scheduled
.\.venv\Scripts\python.exe -m taskagent import-scheduled '<结果包绝对路径>'
```

扫描命令仅扫描来源，不重跑原定时任务。CLI 输出各来源状态；云端未连接时整体结果为 `partial`，即使命令正常结束也不代表云端同步成功。

### 标准收件箱

将完整 JSON 包放到 `W:\message-task-agent\data\inbox\scheduled\chatgpt\*.json`，下次扫描重读全部文件并增量入库。生产者先写 `.tmp`，完成后原子改名为 `.json`。文件不删除，不依赖文件修改时间去重。

也可从界面导入；包先保存至 `data/inbox/scheduled/imports`，随后入库。每包最多 1000 条、HTTP 最多 8 MiB。示例可从面板下载（不要把示例当成真实记录）。

```json
{
  "format": "mta.scheduled-results",
  "version": "1.0",
  "results": [{
    "provider": "chatgpt",
    "account_key": "personal",
    "task_id": "来自来源的稳定任务ID",
    "run_id": "来自来源的稳定运行ID",
    "title": "原任务标题",
    "source_time": "2026-10-20T08:00:00+08:00",
    "run_status": "completed",
    "text": "本次执行的完整正文，不是通知摘要",
    "completeness": "full"
  }]
}
```

`provider`: `chatgpt` / `codex`；`run_status`: `completed` / `failed` / `cancelled`，只接收已结束运行；`completeness`: `full` / `summary`；可选 `thread_id`。所有时间须包含时区。不要随机生成 run_id 来冒充来源身份。

## 数据与去重

- 存储来源为 `codex_scheduled` / `chatgpt_scheduled`，账号 → 定时任务 scope → 运行 item → 正文版本。
- 同来源、账号、任务 ID、运行 ID 标识一条运行。相同规范化结果重复读取不会新建 item/version/event；结果变化增加版本。旧版本重放不会把最新版本倒退。
- Codex cron 首次定时运行用来源线程 ID 标识；后续同线程的其他定时运行用 turn ID。heartbeat 按 turn ID。无本机正文时可保存来源已有的归档正文或通知摘要，身份与后续完整正文保持一致。
- 原始结果保存在 `data/raw/scheduled`，按内容摘要命名；数据库事务提交之后才报告入库成功。摘要不能覆盖已有完整正文。
- `summary` 仅保存，不进入解析事件队列；升级到 `full` 才生成事件。不同 run ID 的相同正文仍是不同来源通知；是否对应同一个业务待办留给后续解析/归并，不在扫描器中凭相似度合并。
- **运行完成 ≠ 业务待办完成。**本模块不写 tasks、event_consumptions 或通知 outbox。完整结果进入既有事件队列，在“数据交换”可导出提示词/原始消息并导入解析结果。提示词 1.1 明确这一边界；旧解析批次继续使用各自快照。
- 扫描记录保存在 `scheduled_scan_jobs`（迁移 8）。SQLite 约束阻止并发全量扫描；异常进程的过期租约可重新扫描。原始来源数据库只读打开并检查路径范围，缺文件/损坏/未写完行明确报出。

## HTTP（仅回环 / 同源）

- `POST /api/scheduled/scan`：202 后台扫描，轮询 status。
- `GET /api/scheduled/status`：连接状态、计数、最新扫描与每源结果。
- `GET /api/scheduled/messages?limit=100`：最新结果列表，最多 500。
- `GET /api/scheduled/messages/<item_id>/raw`：下载最新源记录。
- `POST /api/scheduled/import`：保存并导入标准包。
- `GET /api/scheduled/example`：下载格式示例。

POST 沿用 Host / Origin / X-Requested-With 校验，不提供远程跨域写入。没有新增后台自动周期，也没有改写原来的每 6 小时任务。

## 验证

`tests/test_scheduled_results.py` 与 `tests/test_scheduled_web.py` 覆盖：真实定时触发归属、普通聊天排除、同次结果重放/变化、摘要升级、不降级、不猜测完成、跨日运行保留、源时间、包校验原子性、实时未写完尾行、缺文件、独占扫描、消息交换、HTTP 入库/下载/同源限制。

后续要完成 ChatGPT 网页 / 客户端自动同步，必须先拿到可读取的登录后 Scheduled 页面，验证任务/运行身份、正文分页和历史覆盖范围；未完成前保持“未连接”，不能用空包伪装成功。
