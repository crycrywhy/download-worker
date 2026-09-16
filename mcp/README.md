# downloader-mcp

把一套**已有的 Linux 侧大文件下载系统**包装成 MCP Server：任何 MCP 客户端（Claude Code、
其它 agent 框架）都能用工具调用的方式发起下载、查进度、看下载通道健康状况、查下载台账。

> **本仓库是适配层（Adapter），不是第二个下载器。**
> Range 请求 / Content-Range 校验 / 分块调度 / 重试 / 断点续传 / 校验 / 零串扫描 /
> 流式转发 / 出口选择算法 **全部属于**既有 downloader 脚本与 worker 服务。
> 本 MCP 只做四件事：**参数校验 → 启动既有 downloader 子进程 → 读既有状态产物 → 结构化返回**
> （状态产物 = `<output>.download.json` sidecar / 端口注册表 / 告警流水 / 下载台账 CSV）。

## 1. 它包装的是什么系统

```text
        ┌──────────────┐   MCP (stdio)    ┌──────────────────────────────────────┐
        │ MCP 客户端    │ ───────────────► │ downloader-mcp（本仓库，纯 stdlib）    │
        └──────────────┘                  └───────┬──────────────────────────────┘
                                                  │ 启动 detached 子进程
                                                  ▼
                                       ┌──────────────────────┐
                                       │ linux_downloader.py  │ ← 既有下载器（本仓库只调用，不含其逻辑）
                                       └───────┬──────────────┘
                                               │ HTTP Range（经本地隧道端点）
                          ┌────────────────────┼────────────────────┐
                          ▼                    ▼                    ▼
                  127.0.0.1:8766       127.0.0.1:8767        ……（可扩展）
                   worker 出口 A        worker 出口 B
                （每个出口一套本地隧道；出口在独立网络里，只做流式代理）
```

设计要点：

- **下载走「网络出口」而不是本机直连**：远端 worker 是**无状态流式代理**——Range 头原样透传，
  上游错误真实暴露（404/500 不伪造成 200），不落完整文件、不管断点与调度；状态全在 Linux 侧。
- **一律用本机回环端点 `http://127.0.0.1:<port>`**：隧道把远端 worker 映射到回环口，
  不要直连出口机器的其它地址（跨网段的用户态网络路由不可靠）。
- **出口池**：多个出口并行。一个口掉了，任务回队列由其它口接手；口恢复后自动收回。

## 2. 安装

要求：Python 3.8+，**只用标准库**；系统里已有可用的 downloader 脚本。

```bash
git clone <this-repo> downloader_mcp && cd downloader_mcp
python3 mcp_server.py        # 直接手跑即为 stdio server（日志走 stderr）
```

注册到 Claude Code（`$CLAUDE_BIN` 换成你的 CLI 路径）：

```bash
"$CLAUDE_BIN" mcp add -s user downloader -- /usr/bin/python3 /path/to/downloader_mcp/mcp_server.py
```

其它客户端：按 MCP stdio 约定，用 `command` + `args` 起 `python3 mcp_server.py` 即可。

## 3. 配置

优先级：**环境变量 > 配置文件 > 内置默认**。配置文件默认路径
`~/.config/downloader_mcp/config.json`（可用 `WINDL_CONFIG` 指定别的路径；显式指定但文件
不存在时**不会**回退到默认路径）。文件内容是一个 JSON 对象，键名同下表的小写形式：

```json
{
  "downloader": "/path/to/linux_downloader.py",
  "scripts_dir": "/path/to/download-scripts",
  "worker_url": "http://127.0.0.1:8766",
  "status_csv": "/path/to/download_status.csv",
  "alerts_file": "/path/to/worker_alerts.jsonl"
}
```

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `WINDL_DOWNLOADER` | `<项目>/vendor/linux_downloader.py` | 既有下载器脚本路径。**接入生产时应指向真实脚本**（`vendor/` 那份是随仓库固定的版本副本，供离线测试用） |
| `WINDL_PYTHON` | 本进程解释器 | 运行下载器与辅助脚本的解释器 |
| `WINDL_WORKER_URL` | `http://127.0.0.1:8766` | 单端点模式用的出口端点（没配池时用它） |
| `WINDL_SCRIPTS_DIR` | downloader 所在目录 | 存放 `worker_pool.py` / `worker_alert.py` / `status_collector.py` 的目录 |
| `WINDL_REGISTRY` | `<scripts_dir>/workers.json` | 出口端口注册表（池的成员名单） |
| `WINDL_WORKER_STATUS` | `<scripts_dir>/worker_status.json` | 探活哨兵写的状态快照（含 `down_since`、上线记录） |
| `WINDL_STATE_DIR` | `<scripts_dir>/state` | 完整性/修复状态目录（修复 manifest 等） |
| `WINDL_STATUS_CSV` | `<state_dir>/download_status.csv` | 下载台账 CSV（由既有 collector 生成，**本 server 只读不写**） |
| `WINDL_ALERTS_FILE` | `<state_dir>/worker_alerts.jsonl` | 告警流水 JSONL（掉线/恢复事件） |
| `WINDL_HEALTH_TIMEOUT` | `8` | 出口 `/health` 超时（秒） |
| `WINDL_SCRIPT_TIMEOUT` | `45` | 调辅助脚本（探活/自检/台账刷新）的超时（秒） |
| `WINDL_START_GRACE` | `3` | 启动观察窗（秒）：子进程在这段时间内立即失败，会在本次调用里直接报出来 |
| `WINDL_DEBUG` | — | 设为 `1` 打开 stderr 诊断日志 |

> 路径类配置都有「由 `scripts_dir` 推导」的默认值，通常把它指对就够了；台账与告警流水的位置
> 随部署而异（很多部署把它们放在一个专门的 status 目录），用 `WINDL_STATUS_CSV` /
> `WINDL_ALERTS_FILE` 覆盖。
> 人类可读时间戳一律按 **UTC+8** 渲染（与台账 CSV 的 `updated` 列、哨兵状态文件的
> `updated_utc8` 同口径），字段名统一带 `_utc8` 后缀，避免同一页出现两种时间基准。

## 4. 工具

### `download`

启动一次下载（detached 后台子进程），**立即返回句柄**；长下载（几十~几百 GB）是预期用法。

| 参数 | 说明 |
|---|---|
| `url`（必填） | 绝对 http(s) URL |
| `output`（必填） | 本地输出路径（按原样使用；父目录自动创建） |
| `md5` | 官方整文件 md5（给了就在终验里核对） |
| `chunk_size` | 分块字节数，默认 256 MiB。**不要调小**：每个请求有约十秒的固定开销，小块会被开销拖垮 |
| `connections` | 并发 Range 连接数（省略=下载器默认；`1`=串行） |
| `retries` | 每块最大重试次数（省略=下载器默认） |
| `via_worker` | `true`（默认）经出口下载；`false` 本机直连。**出口不可用时 `true` 会明确报错，绝不偷偷回退成直连** |
| `worker` | `"auto"`（从池里挑一个活口）或池里已登记的端口号；省略=用配置端点。**不接受任意 URL** |
| `max_bytes` | 只取前 N 字节（截断测试用，需源支持 Range） |
| `wait_seconds` | 阻塞等待上限（默认 0=立即返回；小文件可用） |

**续传语义**：重跑**同一条** `url`/`output`/参数即自动从既有 `<output>.download.json` 续传
（已完成块跳过），没有单独的 resume 开关。sidecar 存在期间**不要手动删输出文件**；
参数与 sidecar 不符时下载器会从零重下（已有数据保留、逐块覆写）。

**终验**：大小、md5（给了才验）、零串扫描（≥64 KB 连续零 = 疑似「卡洞」）全部通过才删 sidecar。

### `download_status`

按 `<output>` 与既有 sidecar 报告状态（**不另建 job 数据库**）：
`not_started` / `running` / `incomplete`（有 sidecar = 可续传，含被中断或失败但保留断点的下载）/
`completed` / `failed`（无 sidecar 可续传）。本 server 起的下载还会附带 pid、已运行时间、
日志路径与日志尾部。`with_ledger=true` 时附上台账里匹配的那一行（路径全等，或文件位于
`output` 目录之下；不匹配就 `matched=false`，不猜）。

### `worker_status`

报告哪些出口可达（池模式列出每个已启用端口与 `/health` 延迟；有哨兵状态文件时附 `down_since`、
最近上线/下线时间等历史）。**只读诊断**：不管理隧道、不启停 worker、不碰池子。
`include_history=false` 可只看当前探活结果。

### `alerts`

读告警流水（既有告警哨兵写的 JSONL）：掉线/恢复事件、时间戳、失败端口、错误，以及
（哨兵记录时）本地监听口是否还在——用来区分「隧道断了」与「worker 卡了」。
`ensure=true` 时先跑一次哨兵的**幂等自检**（没在跑才拉起），再返回最近若干条事件。

### `overview`

汇总既有 collector 生成的下载台账 CSV：状态计数（排队/下载中/完成/修复中/已修复/修复失败）、
按状态/物种子串/属过滤，以及完整性一节（修复计数 + 状态目录里的修复 manifest 概览）。
`refresh=true` 时先跑一次 collector（幂等，树大时可能要几秒）再读。**本 server 从不写台账。**

## 5. 错误与边界

- **参数错误**：一次返回所有问题项（`isError: true`，人类可读，不带 traceback）。
- **出口不可用**：`via_worker=true` 时明确报错并提示改用 `via_worker=false`，**不静默回退**。
- **协议版本**：`initialize` 原样回显客户端协议版本（未提供时回退到 `2024-11-05`）。
- **stdout 只走 JSON-RPC**，所有日志走 stderr——stdout 被污染会让客户端解析失败。
- **不做的事**（设计如此）：任意 HTTP 代理 / 任意 URL 拉取、任意出口地址、隧道与 worker 的
  启停管理、在本 server 内重新实现下载或限速逻辑。
- **`worker_status` 的 `available` 只说「端口可达」**：不代表对方此刻能立刻接受新任务
  （出口被大文件占满时，额外的上游请求可能被拒）。真实下载失败会在 `download` 的报错与
  下载器日志里如实暴露。
- 只读工具不改动系统；`ensure` / `refresh` 是**幂等**轻量动作（拉起哨兵、刷新台账），
  重型修复/扫描类动作不通过 MCP 触发。

## 6. 测试

```bash
python3 tests/run_offline_tests.py            # 离线全套（不接触真实出口）
python3 tests/run_live_smoke.py [--ensure]    # 真机只读冒烟（对已部署系统）
python3 tests/run_worker_tests.py [--mb 8]    # 真机下载小样 + 与直连取数逐字节比对
```

离线套件用仓库内 vendored 的 mock（同时扮演源站与假出口，带故障注入）跑真 HTTP，覆盖：
启动 / `tools/list`、普通下载、出口通路、断点续传（中途 SIGKILL）、并发连接映射、
出口不可用不回退、参数错误、重复 output 保护、池状态与选口、告警流水、台账概览、
配置链（env > 文件 > 默认）。

真机脚本不含任何机器特定路径：`run_live_smoke.py` 走部署侧配置，`run_worker_tests.py`
可用 `WINDL_TEST_WORKER=<端口>` 指定某个出口（例如某个出口对某个源站不通时，
用它验证 MCP 链路本身没坏）。

## 7. 已知限制

- `download` 只对**本 server 进程内**已知的子进程做重复提交保护（同一 output 已在跑则拒绝）；
  跨进程并发由下载器自身的 sidecar 语义兜底。
- 池模式下的「选活口」用的是池脚本的探活结果；**探活周期决定出口掉线/恢复的收敛速度**
  （见池脚本自身文档）。
- 台账与告警流水是**只读投影**：本 server 不生成、不修复、不搬运它们。
- `vendor/` 里的下载器副本是**固定版本快照**（见 `vendor/VENDOR.md`），生产部署请用
  `WINDL_DOWNLOADER` 指向真实脚本，以免用到过期版本。
- `worker_status` 的探活只覆盖 `/health`：出口进程活着但**到某个源站的路由坏了**这类
  「活着但不好用」的状态，探活看不出来——表现为具体下载在探测阶段报错（详见 `alerts` 与
  下载器日志）。

## 8. 许可

MIT，见仓库根目录的 [`LICENSE`](../LICENSE)。
