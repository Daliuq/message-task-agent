# 多源增量与任务规划助手（开发中）

工作目录：`W:\message-task-agent`

## 开源与克隆

项目自有代码以 **GPL-3.0-only** 开源，完整条款见 [LICENSE](LICENSE)。第三方依赖、子模块与 UI 动画库保留其原始许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；根目录许可不重新授权这些组件。

```powershell
git clone --recurse-submodules https://github.com/Daliuq/message-task-agent.git
cd message-task-agent
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\run.ps1 init
.\run.ps1 ui
```

QQ 读取需要额外安装隔离环境，微信需要配置本机读取环境；见各组件文档。仓库不包含聊天、邮件、数据库、账号授权信息、密钥或本机验收截图。

## 开发文档

- [v0.2 开发规格（2026-10-20）](docs/DEVELOPMENT_SPEC_V0_2.md)：增量导入导出、解析结果和提示词格式、网页聊天脚本／API 双通道、RAG＋时间归并、作息 JSON 与排程验收。**这是设计稿，不代表新增能力已经实现。**
- [开源复用记录](docs/OPEN_SOURCE_REUSE.md)：区分已采用组件与候选。
- [D1 实现与验收记录](docs/D1_IMPLEMENTATION.md)：文件交换、消费记录和管理界面的当前边界与操作方法。
- [微信 / QQ 聊天导入](docs/CHAT_IMPORT.md)：会话发现、搜索穿梭订阅、本地全量、时间范围、增量、分页续扫和入库验收。

先实现可验证的核心闭环：只读 QQ 邮箱采集 → SQLite 持久化/游标 → 独立 Agent 消费 → 幂等任务归并 → 邮件 outbox。扫描时才读取 Windows 凭据管理器中的 QQ 邮箱授权码；扫描使用 IMAP 只读模式。SMTP 投递仍需显式运行对应命令。

## 已实现（原型 0.1）

- SQLite WAL、外键、事务、扫描记录/游标、消息版本和消费日志。
- QQ Mail IMAP 只读适配器：复用本机现有 Windows 凭据管理器条目 `CodexQQMailClient/primary`；仅在运行 `scan-mail` 或在本地面板中确认扫描时读取。只读枚举可列出的文件夹，按 `UID SEARCH ALL` 扫描，不依赖未读状态，使用 `BODY.PEEK[]` 保存完整 MIME 原文。
- 时间范围全量补扫：按 IMAP `INTERNALDATE` 搜索并精确过滤收件时间，完整读取所选范围所有邮件，分批持久化且幂等，不改动日常增量 UID 游标。
- 每个文件夹独立 `UIDVALIDITY/UID` 游标。新文件夹默认建立“此刻基线”；显式 `--import-history` 才导入该文件夹已有邮件。可用重复的 `--folder` 限定文件夹，`--newest-first` 仅用于有限样本抽取，不用于完整历史回补。每文件夹每轮最多 25 封，后续续扫。
- 原始 MIME 用 SHA-256 内容寻址保存在 `data/raw/email/`。内容、版本、事件和游标在同一数据库事务中提交；事务失败不推进游标。
- 独立事件 Worker、JSON stdin/stdout Agent 协议（`shell=False`）、消费尝试日志与过期租约。
- Agent 以明确的 `identity_key` 做精确任务归并；不同消息使用相同 key 时关联同一任务。未完成事项第二次被独立事件提醒时只新增 `repeat_reminder` 邮件，不另建任务。
- 完成状态不会被旧提醒重开。任务字段变化和状态变化有版本记录。
- 邮件 outbox、预览、稳定 Message-ID、SMTP 投递记录；SMTP DATA 超时标记 `delivery_unknown`，不自动重发；明确失败只允许人工重试。
- CLI 可查看任务/预览和标记完成状态；文件交换使用 `jsonschema==4.26.0` 校验数据格式。
- 可选 OR-Tools CP-SAT 单日排程：只使用用户明确给出的可用时间窗，可传入固定占用时段；根据优先级、截止紧迫性和预计耗时排冲突，未安排事项会保留并说明原因，计划版本写入 SQLite。
- 本机可视化面板：总览、邮件详情、任务状态、扫描/消费记录和通知预览；采用本地打包的 GSAP / ScrollTrigger、非对称概览、消息轮播和滚动动效，尊重系统减少动态效果偏好；无远程字体、图片或脚本请求。服务只绑定回环地址，面板不会自动启动 Agent 或发送邮件。
- 聊天范围发现与选择：聊天订阅页可调用本机微信读取环境枚举账号、会话名称和历史消息数，不读取消息正文；候选结果默认不勾选，用户选中后才写入订阅范围。QQ 复用固定上游组件和已验证的本机密钥发现群聊/私聊。两侧搜索穿梭框可批量移入/移出，保存后生效；移出仅停用订阅，不删正文或标签。已选范围可配置参与全量扫描。
- 聊天多标签分类：在“聊天订阅 → 编辑配置”自定义标签，一个聊天可关联多个标签，一个标签可复用于多个微信/QQ 聊天；支持按标签筛选与标签名称搜索。新聊天默认无标签，标签不改变订阅/扫描开关。数据关系与 API 见 [聊天标签说明](docs/CHAT_TAGS.md)。
- 微信 / QQ 正文导入：已选择会话支持本地全部历史、北京时间日期范围和消息增量；逐分片/主键分页，原始字段（BLOB 以 base64 保存）、正文、事件和检查点持久化。后台任务可查看进度、取消与恢复；支持正文查看、原始字段下载及接入 D1 数据交换。不复制外部媒体文件，不等于云端全历史。

## 验证与运行

Python 3.10+，项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\run.ps1 init
.\run.ps1 status
.\run.ps1 email-preview
.\run.ps1 ui
```

可视化面板默认仅监听本机 `127.0.0.1:8765`，打开 `http://127.0.0.1:8765/`。面板提供总览、已落库邮件、事件/扫描记录、聊天订阅、任务状态和通知预览；点击“扫描邮箱”后会先再次确认，再执行只读增量扫描。页面不会自动消费 Agent 事件，也不会自动发送邮件。动画库随包位于 `src/taskagent/static/vendor/`，版本与授权信息见 `NOTICE.md`。关闭运行面板的终端即可停止服务。

### D1 数据交换（已实现）

更新代码后需重启本地面板服务。进入 **数据交换**：

1. 勾选本地已保存的来源（默认全不选），创建导出批次。
2. 下载 `analysis-request.json`、`prompt.txt`，交给你选择的网页聊天处理；目前不会自动向网页或 API 发送内容。
3. 粘贴或选择返回的 `analysis-result.json`，先预检，再确认应用。
4. 查看已解析、失败和缺失数量；候选存入待确认区，不自动建任务或发送邮件。

支持待解析全量、检查点增量、源时间区间三种选择。这里的“全量”仅指本地尚待解析数据，不会触发邮箱或聊天扫描。文件输出目录为 `data/exchange/<batch_id>/`。第一次使用交换模块前会自动保存数据库一致性备份，迁移只增表和触发器。

命令行同样可用：

```powershell
.\run.ps1 export-batch --key manual-001 --mode pending --limit 100
.\run.ps1 import-preview results .\reply.json
.\run.ps1 import-apply <preview_id>
.\run.ps1 exchange-status
```

CLI 不传 `--scope` 表示所有本地来源；UI 必须显式勾选范围。重复预检/应用同一结果不会重复消费；导出不等于已解析，已解析也不等于事项已完成。中断输出可用 `export-resume <batch_id>` 恢复；取消后通过 `pending` 模式重新打包。

在“聊天订阅”页点击“扫描微信会话”或“扫描 QQ 会话”，程序从本机已登录微信数据中发现账号和会话列表，展示会话名称与历史消息数量；扫描结果及扫描记录写入 SQLite。该按钮只发现会话，不导入正文。候选会话全部默认不选，勾选后移入右侧、点击“保存订阅变更”才写入 `sources` / `source_scopes`，新选范围默认启用订阅但不参与全量扫描。之后在配置页调整开关，进入同页下方的“聊天正文导入”，选择会话并点击“开始导入正文”。QQ 同样支持先发现、选择订阅，再全量/范围/增量导入；独立环境与许可记录见 `integrations/qq/README.md`，仅在本机读取已保存密钥，不需要每次重启 QQ。

微信正文导入命令（仅处理已启用范围；全量/范围还要求开启参与全量扫描）：

```powershell
.\run.ps1 scan-chats --platform wechat --mode full
.\run.ps1 scan-chats --platform wechat --mode range --start 2026-10-01 --end 2026-10-20
.\run.ps1 scan-chats --platform wechat --mode incremental
.\run.ps1 chat-import-status
.\run.ps1 chat-import-resume <job_id>
```

页面启动导入返回后台任务，不等待整个历史扫描完成；可关闭页面后再查看。停止面板进程会中断后台任务，租约过期后可从检查点继续。首次读取需要本机微信读取环境可用，复用路径见 `docs/CHAT_IMPORT.md`。

安装排程可选依赖（命令将使用项目隔离环境 `.venv`）：

```powershell
if (-not (Test-Path .\.venv\Scripts\python.exe)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -e ".[planner]"
```

只在明确提供日程空档后规划，不会假定用户可用时间；`--busy` 表示必须避开的固定占用，日期/时段均按北京时间处理：

```powershell
.\run.ps1 plan --date 2026-10-21 --window 09:00-12:00 --window 13:00-18:00 --busy '14:00-15:00=已确定安排'
```

每个待办的 `estimated_minutes` 必须有值；若候选带 `fixed_start_at` 和 `fixed_end_at`，它会作为可选择的固定时段事项参与冲突求解。数值越小的 `priority` 越高。计划会保存求解器状态；只有 CP-SAT 返回 `OPTIMAL` 时才说明在当前权重和约束模型下已证明最优，未安排事项仍保持待办。此 CLI 不改任务完成状态、不发送通知。

测试（只使用本地临时数据库和假 IMAP/SMTP，不连接账号）：

```powershell
$env:PYTHONPATH = '.\src'
python -m unittest discover -s tests -v
```

邮箱首次运行默认只为所有可列出的文件夹建立 UID 基线，不导入既有历史；之后才读取邮箱：

```powershell
.\run.ps1 scan-mail
```

按北京时间收件日期补扫完整历史区间（开始和结束日期均包含；不推进或重置增量游标，可重复运行）：

```powershell
.\run.ps1 scan-mail-range --start 2026-10-01 --end 2026-10-19
.\run.ps1 scan-mail-range --start 2026-10-01 --end 2026-10-19 --folder INBOX --batch-size 50
```

日期时间也可传 ISO 8601，例如 `--start 2026-10-19T09:00:00+08:00 --end 2026-10-19T12:00:00+08:00`；此时开始点包含、结束点不包含。可视化面板“邮件收件箱”页也提供日期范围全量扫描入口。

只有确认要导入已有邮件时才运行 `scan-mail --import-history`。参数仅影响尚未建立游标的文件夹。授权码来自本机 Windows 凭据管理器；不要写入配置、日志或版本库。

消费落库事件前，配置 Agent 命令，例如：

```powershell
.\run.ps1 worker --command-json '["python","C:\\path\\to\\agent.py"]' --once
```

Agent 从 stdin 收到单个 JSON 事件，stdout 返回 JSON：

```json
{"tasks":[{"identity_key":"organization:role:action","title":"提交申请","description":"","due_at":"2026-10-21T23:59:00+08:00","estimated_minutes":45,"priority":2}]}
```

如果匹配不明确，不应猜测共用 key；后续需要接待确认流程。开发链路可显式用 `worker --noop`，它会记录消费但不会产生任务。

实际 SMTP 投递每次都要单独显式确认：

```powershell
.\run.ps1 send-email --to your-address@example.com --confirm-send
```

该命令最多发送一封队列邮件。SMTP 返回成功只记作“服务器已接受”，不代表用户已读或完成任务。无 `--confirm-send` 不会连接 SMTP。

数据库与原文默认位于项目 `data/`。配置样例在 `config/settings.example.json`。后续若修改配置，应避免把授权码放进去。

## 尚未完成

定时任务结果接入的最新状态见 [docs/SCHEDULED_RESULTS.md](docs/SCHEDULED_RESULTS.md)：已实现 Codex 本机完整结果扫描、幂等入库、ChatGPT 标准结果收件箱与面板；ChatGPT 网页／客户端自动读取仍待连接，不能把本地收件箱扫描当作云端同步成功。

- 已通过真实 QQ IMAP 登录、6 个文件夹只读基线、INBOX 单封真实 MIME 抓取与临时库落库测试；已向保存的 QQ 邮箱本人发送一封 SMTP 测试信并收到服务器 accepted 响应。完整增量 → Agent → 任务 → 邮件闭环、收件箱到达确认仍未验收。
- 规划 Agent/自然语言时间估计、用户锁定/覆盖与排程管理 UI；当前已有 OR-Tools CP-SAT 核心求解、冲突选择、未安排理由和计划持久化。
- QQ Windows 本机会话发现与所选聊天的全量/范围/增量正文采集已接入；尚不覆盖云端漫游和全部特殊消息表。微信已接入本地正文导入；外部媒体复制/OCR/语音识别、微信旧消息改动/删除的实时追踪尚未实现。
- changedetection.io 网站快照适配、语义归并待确认；现有面板仅覆盖邮箱采集和任务、通知查看，不含完整配置与排程编辑。
- Windows 后台启停、备份/恢复，以及与现有定时任务并行验证。

开源候选、许可与复用状态见 `docs/OPEN_SOURCE_REUSE.md` 和 `THIRD_PARTY_NOTICES.md`。QQ 上游通过固定提交的 Git 子模块复用，前端静态资源保留原声明；后续组件须锁定版本、记录许可证并优先通过接口依赖复用。
