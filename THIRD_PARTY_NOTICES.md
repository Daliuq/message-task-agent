# 第三方组件与许可证

本项目自有代码采用 GPL-3.0-only，见根目录 LICENSE。以下组件仍受各自上游许可约束，不因本项目开源而改换许可。

## Git 子模块

子模块链接与固定提交记录在 `.gitmodules`、Git tree 和 `integrations/qq/UPSTREAM.json`。没有修改上游源码；克隆后使用 `git submodule update --init --recursive` 获取原仓库与许可证。

| 组件 | 上游 | 固定提交 | 许可 |
| --- | --- | --- | --- |
| QQNT_Export | https://github.com/Tealina28/QQNT_Export | `714d228dcd52f74b06bec91d4dba6ab856f16cd1` | GPL-3.0，见子模块 LICENSE |
| nt_msg_db_util | https://github.com/QQBackup/nt_msg_db_util | `317024ce2c5e3482fdf9f310a5a7159ab4927e34` | GPL-3.0，见子模块 LICENSE |
| qq-win-db-key | https://github.com/QQBackup/qq-win-db-key | `5c643c39bdc65a7ce3ecc414052fdfd93d5f9107` | 非商用开源协议 第1版；包含非商用限制，不是本项目 GPL 代码的一部分 |
| QQDecrypt | https://github.com/QQBackup/QQDecrypt | `0dba968ebcb9ba23a68571780eb6b53687f77493` | 以该子模块许可与版权声明为准 |

QQ 组件通过独立进程和本地接口调用。根目录 GPL 许可不覆盖以上上游代码或取消其限制。

## 前端静态资源

`src/taskagent/static/vendor/` 内原样保留 GSAP 3.13.0 / ScrollTrigger 及其文件头声明。Copyright (c) 2008–2025 GreenSock. All rights reserved. 适用 [GSAP standard license](https://gsap.com/standard-license)，不是 GPL 重新授权；见该目录 NOTICE.md。

## 安装依赖与本机独立组件

- `jsonschema==4.26.0`：MIT；通过 Python 包依赖使用，保留安装包内许可证。
- `ortools==9.15.6755`（可选）：Apache-2.0；通过 Python 包依赖使用。
- `wechatauto-replica`（本机独立环境，非随本仓库分发）：Apache-2.0；已适配提交记录见 docs/OPEN_SOURCE_REUSE.md。
- QQ 独立 Python 环境的依赖版本见 integrations/qq/requirements.lock；各安装包许可证由上游包保留。

开发规格中出现但未实际集成的候选项目不因此成为本仓库的一部分。
