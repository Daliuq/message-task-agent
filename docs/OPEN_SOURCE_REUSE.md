# 开源复用与来源记录

本目录记录真正被依赖或复制的上游代码；新增复用必须在这里记录仓库 URL、固定版本/commit、许可证、复用范围和本地修改。

## 本阶段

- 主业务胶水为项目自有代码。QQ 上游以固定 Git 子模块复用，前端 GSAP / ScrollTrigger 静态资源原样保留；当前许可清单见根目录 `THIRD_PARTY_NOTICES.md`，QQ 固定提交见 `integrations/qq/UPSTREAM.json`。
- D1 JSON Schema 校验依赖 `jsonschema==4.26.0`，仓库 https://github.com/python-jsonschema/jsonschema ，MIT，使用 Draft 2020-12 Validator 与 FormatChecker；没有修改或复制上游源文件，许可证随安装包分发。版本与许可通过 PyPI 元数据核对；仅此直接依赖精确锁定，传递依赖尚未生成完整 lockfile。
- QQ Mail 连接使用 Python 标准库 `imaplib`、`email`；凭据读取兼容现有本地桌面客户端的 Windows Credential Manager 条目 `CodexQQMailClient/primary`。
- 排程使用 PyPI `ortools==9.15.6755`，作为 `planner` optional extra 精确锁定；调用 CP-SAT Python API 建立可选 interval 与 `NoOverlap` 约束，没有复制 Google OR-Tools 源文件。上游仓库声明 Apache-2.0。
- 首版按依赖调用优先设计；不把无法确认许可证的源码直接复制进来。
- 微信正文读取复用本机 editable 安装的 `wechatauto-replica==1.2.6.1`，仓库 https://github.com/fanyuantaier/wechatauto-replica ，本机干净 checkout 的 commit `62503d76a0db069171cb4e42e87090ad285db489`；本地 LICENSE 为 Apache-2.0。通过独立 Python 子进程调用 `WeChatDB` 及封装后的 `_message_dbs/_open/_msg_row_to_dict` 等接口，没有修改/复制上游文件。验收时 `wechatauto/db.py` SHA-256 为 `9d4eba69e868dea8000d8acdbaed723149cf8732c52e097f23fb3b07fa5ddc55`。私有 API 版本变化必须回归，不能假定任意新版兼容。

## 其他候选（尚未集成）

| 项目 | 仓库 | 计划用途 | 许可/状态 |
|---|---|---|---|
| changedetection.io | https://github.com/dgtlmoon/changedetection.io | 网页快照、差分与动态页面浏览器适配 | 上游声明 Apache-2.0；待确定 API/版本和运行方式 |
| NapCatQQ | https://github.com/NapNeko/NapCatQQ | QQ 协议适配候选 | 许可证和可复用边界待核实；离线历史补齐能力需实测 |
| Lagrange.Core | https://github.com/LagrangeDev/Lagrange.Core | QQ 协议适配候选 | 许可证/版本待核实；未作为依赖 |
| Huginn | https://github.com/huginn/huginn | 网站事件规则设计参考 | MIT；先作设计参考，不并行部署完整平台 |

## 许可维护要求

未确认上游当前许可证和具体版本前，不从该项目复制源文件；复制或引入依赖后，再补充固定 commit、许可证原文、版权声明、修改说明与 NOTICE。`PLAN.md` 中候选资料不等于已采用或已验收。
