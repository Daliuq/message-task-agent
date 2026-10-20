# 微信 / QQ 聊天发现、订阅与正文导入

2026-10-20 更新：微信和 Windows NTQQ 本地发现、已选范围全量/范围/增量读取已接入；发现列表与正文导入分开执行。

## 使用

1. “聊天订阅 → 扫描微信会话 / 扫描 QQ 会话”只发现列表，不读取正文。
2. 在搜索穿梭框左侧勾选、移入右侧并保存订阅变更。两侧支持按名称、群号、账号、标签搜索，平台/类型筛选；新聊天默认不选，不需手填 ID。移出仅停用订阅，保留历史和标签。
3. “编辑配置”启用订阅；全量/时间范围还需启用参与全量扫描，可配置多个自定义标签。
4. 同页下方“聊天正文导入”，选择平台和本地全部历史、时间范围或新消息增量，勾选聊天后开始。
5. 查看任务进度、原始时间覆盖、已保存条数及失败原因；页面底部可展开最近 50 条正文、下载原始字段。其他已保存记录仍在数据库，不是只导入 50 条。
6. 前往“数据交换”导出已入库微信 / QQ 消息，继续独立 LLM 处理。

全量指所选聊天在本机当前可读取分片中的全部记录，不代表全部账号、手机或云端漫游历史。范围按源 create_time 过滤，北京时间日期起止日均包含；ISO 时间戳按 `[start,end)`。时间未知的记录全量仍保留，但范围扫描排除，并显示源未知时间计数。

## 保存内容与边界

- SQLite 主库：`data/taskagent.sqlite3`。`source_items` 存消息身份，`item_versions` 存正文、元数据和原始文件路径，`events` 存独立待消费事件。
- 原始字段：`data/raw/chat/<sha256前两位>/<sha256>.json`。保存消息表原始列；BLOB 无损转为 `$base64`。正文解码由本机 wechatauto 提供，不支持的类型保留占位和原始数据。
- 不复制消息引用的外部图片、视频、语音附件；没有 OCR 或语音转写。不能把“消息记录已保存”说成“全部媒体已备份”。
- 导入不运行 LLM、不消费事件、不创建正式任务、不发送邮件、不修改原始微信数据库。
- 以已有服务的隔离缓存打开本机库，只查询所选聊天消息表；为了读取消息表，底层库仍可能需要解密整个相关分片到临时缓存。缓存目录在工作结束后清理；不向网页或外部 API 发送聊天数据。

## 增量、分页与恢复

迁移 7 添加 `chat_import_jobs`、`chat_import_scopes`。任务配置、源快照边界、每分片位置和计数持久化。

- 使用分片和 local_id 的 keyset 分页，不使用变化中的 OFFSET，也不以不唯一的 sort_seq 排除旧序号。同序号消息、不同分片同 local_id 不会互相覆盖。
- 消息身份优先 server_id，缺失时使用分片 + local_id；身份始终限定在账号/会话内部。
- 每页先校验/保存原始文件，再在同一个 SQLite 事务中提交消息、版本、事件、扫描记录与检查点；提交成功才 ACK 子进程下一页。
- 断点续扫固定原任务的分片上界，不把新消息混入旧任务；源边界被替换、分片缺失或数量不符时失败，不伪造完成。新分片由后续增量/新全量任务读取。
- 全量导入及增量推进逐分片增量检查点；时间范围补扫不推进日常增量游标。增量第一次运行无基线时读取本地既有记录。
- 重复全量导入不重复新增相同版本或事件；已存在消息的内容改变会记录新版本。微信增量只追踪新主键；QQ 增量核对稳定消息身份及全部原始列指纹，发现较小 ID 的补同步和旧消息内容变化。两者不自动删除源库已删除记录。
- 同一会话不能被多个活动导入占用；进程租约 90 秒，正常运行每 15 秒续期。异常退出后等待租约过期，再点击“从检查点继续”。旧进程不能覆盖新持有者状态。
- 取消保留已保存页；读取期间也会检查取消和订阅状态。取消任务不能恢复，可新建导入并通过身份去重。

HTTP 创建返回 202，前端轮询任务状态，避免全量扫描挂在一个长请求上造成 502。

## CLI / HTTP

```powershell
.\run.ps1 scan-chats --platform wechat --mode full --batch-size 200
.\run.ps1 scan-chats --mode range --start 2026-10-01 --end 2026-10-20
.\run.ps1 scan-chats --mode incremental
.\run.ps1 chat-import-status --job <job_id>
.\run.ps1 chat-import-resume <job_id>
```

可重复 `--scope <scope_id>` 指定已配置范围；省略使用符合开关条件的已订阅微信范围，不会扫描未选择会话。

- `POST /api/chat-imports`：platform、mode、scope_ids、start/end、batch_size。
- `GET /api/chat-imports` / `GET /api/chat-imports/{id}`：进度、分片覆盖、检查点、失败原因。
- `POST /api/chat-imports/{id}/resume` / `cancel`：恢复/取消。
- `GET /api/chat-messages?scope_id=...&limit=50`：最近正文，最多 200。
- `GET /api/chat-messages/{item_id}/raw`：下载当前版本原始字段 JSON。
- `GET /api/qq-local-status`：QQ 默认目录账号消息库文件状态，不返回密钥或聊天正文。

## 读取环境

复用 `WECHAT_PYTHON` 指定的解释器，或本机 `wei-x/work/wechatauto-env/Scripts/python.exe`。独立子进程便于隔离原有读取环境和本程序依赖。本机已安装版本/commit/许可记录见 `OPEN_SOURCE_REUSE.md`，桥接只针对该已验证版本；上游私有接口变化需回归。

## 真实验收

正式库运行前已保存 SQLite 一致性备份：`data/backups/before-chat-import-20261020T083626Z.sqlite3`。

对用户当时已配置的 1 个启用微信会话：

| 扫描 | 实际读取 | 新增消息 | 新增版本 | 状态 |
|---|---:|---:|---:|---|
| 首次本地全量 | 292 | 292 | 292 | completed |
| 重复本地全量 | 292 | 0 | 0 | completed |
| 随后增量 | 0 | 0 | 0 | completed |

该快照的原始时间范围为北京时间 2026-08-31 16:34 至 2026-10-20 12:54。不是声称此时间段的手机/云端数据全部存在。任务与通知仍为 0，未调用模型或发送消息。

证据摘要（不含正文）：`verification/chat-import-live-summary.json`。浏览器合成数据验收截图：`verification/chat-import-synthetic.png`。覆盖全部分片、同序号、时间范围、未知时间、重复导入、内容版本变化、事务回滚、失败恢复、取消、租约竞争、源重置、HTTP、D1 往返等测试。

最终全量回归 92 项通过；Python compileall、app.js/chat-import.js 的 Node 语法检查通过。浏览器合成验收页未记录 console error；新增取消/租约校验后再跑真实增量，仍 completed、读取/新增均为 0。

## QQ 本地适配（2026-10-20）

默认发现 `Documents/Tencent Files/<账号>/nt_qq/nt_db/nt_msg.db`，通过当前用户 DPAPI 解密已保存密钥，工作进程只查询一致快照副本。复用项目、许可与固定版本见 `../integrations/qq/README.md` 和 `UPSTREAM.json`。

真实面板 HTTP 已发现 203 个 QQ 会话（95 群聊、108 私聊），不自动加入新订阅。
当前用户已选 8 个 QQ 订阅；正式库已有用户启动的 5 会话全量任务 completed，新增 244 条。
本次界面测试未替用户变更上述订阅，也未另起真实正文导入。

合成浏览器验收：搜索、筛选下保留勾选、左右移动、原子保存、停用不删标签；
QQ 全量 3 条后增量 0 新增。专项单元测试另覆盖迟到较小主键与内容更新。
最终回归 106 项通过，Python 和 JavaScript 语法检查通过。
截图 `verification/qq-transfer-synthetic.png`，不含真实私聊。

新增接口：`POST /api/subscriptions/transfer`，接收 candidate_ids、enable_ids、disable_ids、full_scan_enabled。
仅对显式增减做一个事务；过期发现结果或非法 ID 会整批回滚，不启动消息导入。
