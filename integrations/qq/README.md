# QQ 本地读取组件

本组件复用 `vendor/nt_msg_db_util/1.decrypt.py` 的 SQLCipher 开库参数，以及
`vendor/QQNT_Export` 的会话分区、Protobuf 解析和正文格式化。未改上游源文件。
固定提交与许可见 `UPSTREAM.json`；各仓库原始 LICENSE 保留在各 vendor 目录中。
其中取钥工具 qq-win-db-key 是非商用许可，不应当作 MIT 组件重新发布。

## 运行环境

GitHub 源码中的 `vendor` 使用 Git submodule 固定上游提交；克隆时加
`--recurse-submodules`，已有克隆执行 `git submodule update --init --recursive`。
本机虚拟环境、账号密钥、聊天数据库和验收截图不随仓库上传。

独立 Python 3.10 环境 `.venv`；依赖固定在 `requirements.lock`。
主程序经 stdin 传入密钥与请求；工作进程仅返回会话列表或已选择会话的分页。
不会把密钥放进命令行、URL、日志或消息导出。

## 首次取钥

`setup_key.ps1` 必须由用户明确同意启动 QQ 后执行，并先等待 QQ 退出。
它调用固定版本的上游工具，验证群聊/私聊表及样本页，然后将密钥以当前 Windows
用户 DPAPI 加密，保存到 `data/qq/<account>.dpapi.json`。不宣称做过全库完整性校验。
后续发现与扫描只使用已存密钥，无需重启或注入 QQ。

## 读取约定

- 默认 `Documents/Tencent Files/<account>/nt_qq/nt_db`；开发时可设置 QQ_DATA_ROOT。
- 对变化中的源文件作大小/修改时间前后比对；消息库与 WAL 一起复制到私有临时目录。
  所有 SQLCipher / SQLAlchemy 连接只打开副本，设置 query_only，不修改 QQ 原库。
- 发现群聊、私聊及本机消息数量。发现不创建订阅、不持久化正文。
- 全量/范围只读已启用且允许全量的会话；增量只读已启用会话。
- QQ 的原始主键不能假定单调，增量以稳定消息身份和原始字段 SHA-256 指纹核对。
  因而每次会读取所选会话的原始记录，但只解析、持久化新增或变化的消息；
  不按发送时间忽略迟到同步或较小主键。
- 续扫持久化任务初始指纹基线、快照摘要和已提交位置；源变化无法核对时明确失败。
- 保留所有原始列，BLOB 以 base64 保存；未解析内容也保留，不过滤空占位行。
- 仅覆盖当前本机 group_msg_table 与 c2c_msg_table；不声称包含云端漫游、
  数据线、频道、服务助手或所有其他消息表。媒体文件不复制、不转写。

## 测试

主环境运行：`python -m unittest discover -s tests -q`。
QQ 专项测试复用本独立环境运行实际上游解析器，输入均为合成 SQLite / Protobuf；
包含负数主键、迟到消息、旧消息修改、范围、重复扫描、恢复和私聊。
