# Local Download Worker

把一台 Windows PC 变成「下载出口」：它在自己的机器上发起下载，再通过 **SSH 反向隧道** 把 HTTP 服务暴露给 Linux 侧使用。
适用场景：服务器直连某些数据源（ENA / NCBI / GenomeArk 等）慢或被限流，而 Windows 家宽出口快得多。

**English quick start** — 见下方 [English quick start](#english-quick-start)；本文档其余部分为中文。

---

## 本仓库包含两部分

| 目录 | 是什么 | 文档 |
|---|---|---|
| **仓库根目录**（`installer/`、`worker/`） | **Windows 侧**：把一台 Windows PC 装成下载出口（本 README 的主体，从下一节开始） | 本文件 |
| **`mcp/`** | **MCP 适配层**：把 Linux 侧调用这套出口的下载系统包装成 MCP Server，任何 MCP 客户端都能用工具调用发起下载、查进度、看出口健康状况 | [`mcp/README.md`](mcp/README.md) |

两者可以分开用：只想要「Windows 家宽当下载出口」，看完本文件就够了；
想让 agent（Claude Code 等）直接调下载，再看 `mcp/`。

---

## English quick start

On the Windows PC that should become the download outlet:

1. **Prerequisites**: Python 3.8+ (with `venv`), [Tailscale](https://tailscale.com/) installed **and logged in**, Windows OpenSSH client, and passwordless SSH from that PC to your Linux box (`ssh -o BatchMode=yes <user>@<host> echo ok` prints `ok`).
2. **Install** — copy this folder to the PC and run (a UAC prompt appears; it also asks once about the Worker's own proxy and once before touching the execution policy):
   ```
   installer\install.cmd -LinuxUser <linux-user> -LinuxHost <linux-host> -LinuxTunnelPort <端口> -WorkerProxy http://<proxy-ip>:<端口>
   ```
   `-WorkerProxy` is optional: downloads are tried through it first and fall back to DIRECT; without it the Worker goes DIRECT only. It is the Worker's *own* proxy, dialled directly — the PC's private proxy (loopback) is refused. Change it later with `download-worker proxy <url>`.
3. **Verify** — `installer\verify.cmd` (exit code `0` = everything checked out).
4. **Use it from Linux** — `curl http://127.0.0.1:<端口>/health` -> `{"status":"ok","worker":"local-download-worker"}`.
   Data plane: `GET /stream?url=<public-url>` (passes `Range` through) and `POST /download` — see [HTTP API](#http-api).
5. **Measure this PC** — `download-worker test 268435455` pulls 256 MiB through the configured exit (add `-Direct` to compare the direct route) and prints the real speed; nothing is written to disk.

---

## 它解决什么问题

```
调用方（Linux 上的脚本 / 客户端 / curl）
    │  HTTP   http://127.0.0.1:<LinuxTunnelPort>
    ▼
Linux 127.0.0.1:<LinuxTunnelPort>
    │  SSH 反向隧道（Windows 主动连出，ssh -R）
    ▼
Windows Worker <本机 Tailscale IP>:<WorkerPort>        <- FastAPI
    │
    ▼
Windows 本地网络出口 -> ENA / NCBI / GenomeArk / ...
```

两个关键点：

* **隧道方向是 Windows -> Linux**（`ssh -R`）。Windows 侧主动连出，所以它可以在 NAT / 防火墙后面，路由器不需要端口映射，Windows 也不需要公网 IP。
* **Windows 侧只监听自己的 Tailscale 地址**，并由防火墙规则限定只有 Tailscale 网段（`100.64.0.0/10`）能访问 —— 不对局域网或公网开放。

## 特性

| 特性 | 说明 |
|---|---|
| 一条命令安装 | `installer\install.cmd`：自动请求管理员权限、绕过 PowerShell 执行策略限制，无需手工配置 |
| 全配置化 | 本机 Tailscale IP、端口、Linux 用户/地址、日志目录全部来自 `worker-config.json`，包内没有任何机器特定值 |
| 幂等 / 可升级 | 同一条命令重跑即升级；配置、虚拟环境、日志、带宽设置全部保留 |
| 无窗口 | 两个长驻计划任务以 **S4U**（session 0）运行，不会出现控制台窗口，也不可能被误关 |
| 自动恢复 | Worker 退出后 5 秒自动重启（`worker_supervisor.py`）；**隧道**同样有守护（`tunnel_supervisor.py`，退出即重启、5/10/15/20/30 秒退避），计划任务本身也带失败重启 |
| 手动开关 | `download-worker on` / `off` / `status`：随时把这台 PC 从下载池里摘出去或放回来（含 Linux 侧状态同步，不留假告警） |
| 带宽控制 | 0-100%（0 = 暂停），`Get-WorkerBandwidth` / `Set-WorkerBandwidth` 两个命令 |
| 日志轮转 + 回传 | 单文件 10 MB x 5 轮转；按周期增量同步回 Linux 侧 |
| 收敛的攻击面 | 只监听 Tailscale 地址 + 防火墙限定网段；`/stream` 只允许解析到**公网** IP 的 http/https 目标（拦 SSRF） |
| 干净卸载 | 删计划任务、防火墙规则、安装目录；不碰 Tailscale、SSH 密钥、Windows OpenSSH |
| 装前自动清旧版 | 安装时自动找出旧安装（计划任务指向的目录 + 常见路径），停掉其残留进程（venv python + 无名 ssh 隧道）再删除，避免「文件被占用删不掉」「旧隧道占着端口导致新守护起不来」 |

## 前置条件

| 项目 | 要求 | 说明 |
|---|---|---|
| Windows | Windows 10 / Server 2016 或更新，64 位 | 安装器会检查 |
| PowerShell | 5.1（系统自带） | 由 `install.cmd` 自动提权并绕过执行策略，**不需要手工设置** |
| Python | 3.8 或更新（建议 3.10+，需含 `venv`/`pip`） | 安装器**不自动装 Python**，缺失会明确报错 |
| Tailscale | 已安装且**已登录** | 安装器不装、不登录 Tailscale，只检测并取 IPv4 |
| OpenSSH 客户端 | `C:\Windows\System32\OpenSSH\ssh.exe` | Windows 可选功能，安装器只检测 |
| SSH 免密 | 该 PC 已能用 `ssh -o BatchMode=yes <user>@<linux>` 免密登录 Linux 侧 | 安装器**不包含任何私钥**，缺失会明确报错 |

## 安装

把整个文件夹复制到目标 PC（含 `installer` 与 `worker` 两个子目录），然后：

**方式一（推荐）**：在资源管理器里双击

```
installer\install.cmd
```

它会：

1. 检测到没有管理员权限时，弹出 UAC 并以管理员身份重新打开自己；
2. 用 `-ExecutionPolicy Bypass` 启动 `install-worker.ps1`（批处理文件不受 PowerShell 执行策略约束，所以在出厂默认 `Restricted` 的机器上也能直接跑）；
3. 结束后 `pause`，让你看清安装摘要。

第一次安装必须给出 Linux 侧信息：

```bat
installer\install.cmd -LinuxUser <linux用户> -LinuxHost <linux地址> -LinuxTunnelPort <端口>
```

**方式二（进阶）**：已经在一个**管理员** PowerShell 窗口里（且该窗口能跑脚本）时，直接调安装器：

```powershell
cd <复制路径>\installer
.\install-worker.ps1 -LinuxUser <linux用户> -LinuxHost <linux地址> -LinuxTunnelPort <端口>
```

先干跑看一遍要做什么（不改系统）：

```powershell
.\install-worker.ps1 -LinuxUser <linux用户> -LinuxHost <linux地址> -LinuxTunnelPort <端口> -DryRun
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `-InstallDir` | `C:\ProgramData\LocalDownloadWorker` | 安装目录（可换成别的盘，例如 `D:\LocalDownloadWorker`） |
| `-LinuxUser` | 无（首次必填） | Linux 侧 SSH 用户；重跑时省略则沿用已有配置 |
| `-LinuxHost` | 无（首次必填） | Linux 侧地址（Tailscale IP 或可达的 IP/域名）；重跑时省略则沿用 |
| `-LinuxTunnelPort` | `<端口>` | Linux 侧监听端口。**每台 Windows PC 必须不同**（见下） |
| `-WorkerPort` | `8765` | Worker HTTP 端口（Windows 本机） |
| `-WorkerProxy` | 无（=只直连） | **Worker 自己的代理**，本机直接连它（**不再经 ssh 隧道**），形如 `http://<代理IP>:<端口>` 或 `socks5h://<代理IP>:<端口>`；写进 `worker-config.json` 的 `proxy.url`。**下载永远先走它**（比直连快得多），它连不上才回退直连；**本机私人代理绝不承载下载数据**（回环地址在本脚本和 worker.py 两处都被拒，见「代理出口」）。安装时不带此参数会**交互询问**（无人值守则沿用配置里的值） |
| `-LinuxProxyHost`、`-LinuxProxyPort`、`-LinuxProxyType`、`-LinuxProxyLocalPort`、`-DisableLinuxProxy` | — | **已废弃（r5 反向隧道代理）**：为免旧命令行直接报错而保留，`host`+`port`（+`type`）会被折算进 `-WorkerProxy` 并打印 WARN；`-LinuxProxyLocalPort` 接受但忽略；`-DisableLinuxProxy` 等价于 `-WorkerProxy off` |
| `-LogSyncRemoteDir` | `/home/<LinuxUser>/script/download-worker/log` | 日志同步到 Linux 的目标目录 |
| `-LinuxWorkerPoolPath` | `~/script/download-worker/worker_pool.py` | Linux 侧 worker 池脚本路径。`download-worker on/off` 通过它同步本机在池里的启用状态（见「手动开关」） |
| `-LogSyncIntervalMinutes` | `5` | 日志同步周期 |
| `-SkipLogSyncTask` | 关 | 不创建日志同步计划任务 |
| `-PipIndexUrl` | 无（PyPI） | pip 镜像，如 `https://pypi.tuna.tsinghua.edu.cn/simple` |
| `-FixExecutionPolicy` | 关 | 无人值守：新窗口若因执行策略无法加载带宽命令，直接放开当前用户策略（不再询问） |
| `-SkipExecutionPolicyFix` | 关 | 从不询问、也不修改执行策略，只在需要时打印提示 |
| `-SkipLegacyCleanup` | 关 | 跳过「清理旧版本」这一步（自动清理误伤了不该动的目录时才用） |
| `-KeepPaused` | 关 | 保留「主动下线」状态：不动 `paused.flag`，也不做启动验证（确实要让这台 PC 保持离线时用）。不加它就是**默认把本机重新上线** |
| `-DryRun` | 关 | 只打印将要执行的动作，不改系统 |

Windows 侧 Tailscale IP **自动检测**（`tailscale ip -4`，失败则回退解析 `ipconfig` 里 `100.64.0.0/10` 的地址），不写死任何机器的 IP。

### 安装器做了什么

1. 检查管理员权限 / Windows 版本 / 64 位
2. 检查 Python（含 `py` 启动器、PATH、常见安装路径）、`python -m venv`、pip
3. 检查 Tailscale 并取本机 Tailscale IPv4
4. 检查 OpenSSH 客户端，并做一次 `ssh -o BatchMode=yes` 免密认证测试
5. **清理旧版本**：找出旧安装（计划任务指向的目录 + `D:\local-download-worker` 等常见路径，且确实含 Worker 文件），停掉其残留进程（目录下的 python/pythonw + 持有 `-R <隧道口>:` 的 ssh），删除旧目录（最多重试 5 次），**并复核没有进程还占着隧道口**
6. **处理「主动下线」标记**：若存在 `paused.flag`（曾被 `download-worker off` 下线），默认删除它、把本机重新上线；加 `-KeepPaused` 则保留标记并跳过启动验证
7. 创建 `<InstallDir>`，复制 Worker 源码
8. 创建 `<InstallDir>\.venv`，`pip install -r requirements.txt`
9. 创建 `logs\`，写入 `bandwidth.json`（`{"percent": 100}`，UTF-8 **无 BOM**）
10. 写入 `worker-config.json`（本机全部可变配置）。写入前会**问一次 Worker 代理**（`-WorkerProxy` 已给则不问；交互终端才有此题，直接回车沿用当前值，输 `off` 关闭），写完顺手探测一次该代理通不通（不通只 WARN，不中断安装）
11. 建防火墙规则：仅 `100.64.0.0/10`（Tailscale）可访问 `-WorkerPort`
12. 建 3 个计划任务（见下）
13. 注册两个带宽命令（见「带宽控制」），并生成 `download-worker` 开关命令 + 写入用户 PATH（见「手动开关」）
14. 启动 Worker -> 轮询 `http://<Tailscale IP>:<WorkerPort>/health`（最多 90 秒）
15. 启动隧道守护 -> 检查守护与 ssh 进程（**确认端口上的 ssh 确实出自我们的守护；若发现游离隧道就杀掉并等守护接管，最多 90 秒**）-> 从 Linux 侧 `curl http://127.0.0.1:<LinuxTunnelPort>/health` 验证
16. 打印安装摘要（`READY`）

失败时**不会**留下「看起来装好了」的状态：安装器以非零退出码结束，并打印安装日志 / Worker 日志 / 隧道日志的位置。

## 安装后产生的东西

```
<InstallDir>\
|-- worker.py  worker_config.py  start_worker.py  start_tunnel.py
|-- worker_supervisor.py  tunnel_supervisor.py  sync_log.py
|-- download-worker.ps1  WorkerBandwidth.psm1  requirements.txt
|-- worker-config.json          <- 本机配置（升级时保留）
|-- paused.flag                 <- 仅当 `download-worker off` 时存在（本机主动下线标记）
|-- .venv\                      <- 独立 Python 环境（升级时保留/复用）
`-- logs\
    |-- worker.log  worker.log.1..5   <- Worker 日志（轮转 10 MB x 5）
    |-- worker_supervisor.log         <- Worker 守护日志（启动/退出/暂停）
    |-- tunnel.log                    <- SSH 隧道日志
    |-- tunnel_supervisor.log         <- 隧道守护日志（重启与退避都记在这里）
    |-- bandwidth.json                <- 带宽百分比（升级时保留）
    |-- worker.log.sync-state         <- 日志同步断点
    `-- installer.log                 <- 安装器日志
```

另有一个开关命令落在用户目录（见「手动开关」）：

```
%USERPROFILE%\bin\download-worker.cmd   <- 生成的转发器；%USERPROFILE%\bin 会加入用户 PATH
```

`worker-config.json` 示例：

```json
{
  "worker_host": "100.x.y.z",
  "worker_port": 8765,
  "linux_user": "<linux用户>",
  "linux_host": "<linux地址>",
  "linux_tunnel_port": <端口>,
  "linux_worker_pool_path": "~/script/download-worker/worker_pool.py",
  "log_sync": {
    "enabled": true,
    "remote_dir": "/home/<LinuxUser>/script/download-worker/log",
    "interval_minutes": 5
  },
  "proxy": {
    "url": "http://<代理IP>:<端口>",
    "retries": 2,
    "windows_private": { "enabled": false },
    "large_file_threshold_bytes": 1073741824
  }
}
```

`proxy.url` 空串 = 只走直连；可填 `http://host:port`、`socks5://host:port`、`socks5h://host:port`（后者由代理解析 DNS）或带凭据的 `http://user:pass@host:port`（凭据只进客户端、不进日志）。改它最省事的办法是 `download-worker proxy <url>`，**下个请求即生效**（worker.py 每次请求都重读配置，不用 restart）。
`worker_host` 留空或写 `"auto"` 时，Worker 启动时会**自动检测**本机 Tailscale IPv4。
`linux_worker_pool_path` 只被 `download-worker on/off` 的状态同步用到（旧配置没有这一项时回退到安装器默认值）。

## 计划任务（Task Scheduler）

| 任务名 | 程序 | 参数 | 触发 | 登录类型 | 窗口 |
|---|---|---|---|---|---|
| `Local Download Worker` | `.venv\Scripts\python.exe` | `worker_supervisor.py` | 登录时（+15 秒） | **S4U** | **无窗口** |
| `Local Download Tunnel` | `.venv\Scripts\pythonw.exe` | `tunnel_supervisor.py` | 登录时（+45 秒） | **S4U** | **无窗口** |
| `Local Download Log Sync` | `.venv\Scripts\pythonw.exe` | `sync_log.py` | 登录时 + 每 N 分钟 | Interactive | 无窗口（pythonw，短命进程） |

三者共用：`RestartCount=3` / `RestartInterval=PT1M` / `ExecutionTimeLimit=PT0S`（不限时）/ `MultipleInstances=IgnoreNew`，以当前用户身份、最高权限运行。
Worker 自身的进程级恢复由 `worker_supervisor.py` 负责（退出后等 5 秒重启），**隧道**由 `tunnel_supervisor.py` 负责（ssh 退出即重启，间隔按 5/10/15/20/30 秒退避、稳定跑满 60 秒则重置；秒退时会把 `tunnel.log` 的最后一行 ssh 报错抄进自己的日志），不引入第三套 watchdog。

> **为什么隧道也要守护**（2026-09-14 的一次真实事故）：任务计划自带的重启只在**任务失败退出**时生效；ssh 若自己正常退出（换网络 / 掉线 / 被对端断开），任务状态会停在 `Ready` 而没人拉它 —— 表现就是「PC 在线、worker 也在，但 Linux 侧那个端口连不上」。守护脚本把「拉起 ssh」变成一个永不退出的循环，同时它也顺带做到「开机/登录后一定会有隧道」。两件事都由计划任务 + 守护脚本闭环。

> 守护脚本同样尊重「手动开关」：`download-worker off` 期间 worker 守护与隧道守护都不拉起进程（见下节）。

> **隧道自己的探活设置**（`worker/start_tunnel.py` 里那几条 `-o`）：`ServerAliveInterval=15` + `ServerAliveCountMax=3` = 链路静默死掉后 **~45 秒**内 ssh 自己退出（有数据在流时探测会被抑制，所以短间隔不影响下载速度，只决定**空闲**隧道能在死链上挂多久）；`TCPKeepAlive=yes` 让系统层再看一道；`ConnectTimeout=15` 给建连也封顶，避免黑洞路由让 ssh 卡在 `connect()` 里、守护以为它还活着；`ExitOnForwardFailure=yes` 让「端口被占」这类问题立刻失败而不是假装健康；`LogLevel=VERBOSE` 把断开原因（`Timeout, server ... not responding`、`remote port forwarding failed`、`Connection reset by peer`…）留在 `tunnel.log`，否则日志只剩一句 `exit 255`，事后无从诊断。
>
> **客户端的探活救不了 Linux 侧的端口**：链路静默死亡时 Linux 那端收不到 FIN，只能等 TCP 自己超时，这期间端口仍被半开会话占着 —— 新隧道会以 `remote port forwarding failed` 立刻失败（守护因此退避重试，直到对端放开）。把这段等待缩短要靠 **Linux 侧**（识别并清掉端口上已死的 ssh），不属于本包。

**为什么完全没有窗口**（两层保证）：

1. **登录类型 `S4U`**（"不管用户是否登录都运行、不存储密码"）：两个任务都运行在 **session 0**，那里根本没有桌面 —— 即使是控制台子系统的 `python.exe` 也不会显示窗口，更不存在"被误关的窗口"；注销/锁屏后任务继续运行（重新登录时 AtLogOn 触发器只是再确认一次）。
2. **子进程也隐藏**：Tunnel 的启动器是 GUI 子系统的 `pythonw.exe`，且 `start_tunnel.py` 拉起 `ssh.exe` 时带 `CREATE_NO_WINDOW`。Worker 用 `python.exe` 而不是 `pythonw.exe`，因为它跑在 session 0 里本来就没有窗口，这样日志与 stdout 行为跟常规 Python 程序一致。

> 若目标机不允许 `S4U` 登录（缺"作为批处理作业登录"权限），安装器会打印 WARN 并自动退回 `Interactive`；此时 Tunnel 用 `pythonw.exe` 仍然无窗口，Worker 会重新出现一个控制台窗口（仅在这种情况下）。
>
> **退回 `Interactive` 还有一个更隐蔽的后果**：任务进程会落在"启动它的那个登录会话"里——如果那个会话是 SSH / RDP，**退出该会话时 Windows 会把隧道一起带走**（2026-09-14 实测症状：从 shell 登录再 exit，worker 的隧道跟着关）。安装器会在安装日志里回读并打印 `LogonType = S4U/Interactive`，`verify.cmd` 也会显示，一眼可查；要修就给该账号"作为批处理作业登录"权限（`secpol.msc` → 本地策略 → 用户权限分配）后重跑安装器。

> 第三项 `Local Download Log Sync` 是**短命任务**：每 N 分钟（默认 5）启动一次，一秒钟左右同步完就退出，所以平时去看它永远是 `Ready`——那是正常的，不是没跑。
> 它把 `worker.log`（含 `.1`–`.5` 轮转）以及 `tunnel.log`、`worker_supervisor.log`、`tunnel_supervisor.log` **增量**推回 Linux 侧的 `log_sync.remote_dir`（断点存在 `logs\worker.log.sync-state`）；远端目录不存在时脚本会先 `mkdir -p` 建好（2026-09-14 修：此前远端目录一缺就静默失败、日志从没到过 Linux 侧）。不需要这个任务就用 `-SkipLogSyncTask` 关掉。

## 验收

```powershell
installer\verify.cmd          # 只读检查，退出码 0 = 全部通过
```

检查项：安装目录与源文件、venv 与 Python 版本、fastapi/httpx/uvicorn 是否装好、`worker-config.json`、`logs\`、`bandwidth.json`、三个计划任务状态、防火墙规则（端口/远端地址）、带宽命令是否真的能跑（会开一个全新 PowerShell 进程执行一遍）、**`download-worker` 开关命令与用户 PATH**、**隧道任务是否已指向 `tunnel_supervisor.py`**、**隧道守护进程在跑**、`/health`（Tailscale IP 与 localhost 各一次）、隧道 ssh 进程、**从 Linux 侧经隧道访问 `/health`**、Linux 日志目录；若存在 `paused.flag` 会打印 NOTE 提醒这是人为关机。

端到端真实下载在 Linux 侧做：

```bash
curl -s -m 5 http://127.0.0.1:<端口>/health          # 期望 {"status":"ok",...}
curl -s "http://127.0.0.1:<端口>/stream?url=https://example.org/big.tar" -o big.tar
```

## 升级 / 重跑

同一个命令重跑即可，安装器是幂等的：

* **刷新**：Worker 源码、依赖（pip install）、防火墙规则、三个计划任务、带宽命令
* **保留**：`worker-config.json` 中未在命令行重新指定的项、`logs\bandwidth.json`、`logs\` 内容、`.venv`（能跑就复用，坏了才重建）
* **自动清理**：装之前先清掉旧安装（见「安装器做了什么」第 5 步）。若旧版本装在**另一个目录**（例如旧的 `D:\local-download-worker`），它的目录会被删掉；如果它和当前 `-InstallDir` 是同一个目录，则只停进程、保留目录（配置 / 日志 / venv 继续用）
* **重新上线**：若这台 PC 曾被 `download-worker off` 下线（存在 `paused.flag`），重跑安装器会删掉标记、把机器放回下载池；确实要让它保持离线就加 `-KeepPaused`
* 重跑后会重启 Worker 与隧道任务

## 卸载

```powershell
installer\uninstall.cmd                       # 或 uninstall-worker.ps1（管理员 PowerShell）
.\uninstall-worker.ps1 -KeepFiles             # 只删任务和防火墙，保留安装目录
```

删除：三个计划任务、`Local Download Worker*` 防火墙规则、注册的带宽命令、`%USERPROFILE%\bin\download-worker.cmd` 开关命令（`bin` 目录若因此变空，会一并从用户 PATH 里摘掉；里面还有别的东西则保留并 WARN）、安装目录（会先做「看起来像不像安装目录」的校验；不像则拒绝删除，除非 `-Force`），并停掉残留进程 —— 判据是**可执行文件在安装目录下**（venv 的 python/pythonw）或命令行里出现该目录，加上**持有 `-R <隧道口>:` 的 ssh**（隧道进程的命令行里没有目录，只能按端口找）；目录删除失败会自动重试 5 次并列出仍被占用的文件。
**不会**碰：Tailscale、SSH 私钥、Windows OpenSSH、Linux 侧 sshd 配置 —— 这些属于系统环境，不属于本工具。

## 带宽控制

安装器会把这两个命令注册到当前用户的 PowerShell 模块路径，新开一个 PowerShell 窗口直接敲就能用（不需要 `Import-Module`，也不需要知道装到了哪个盘）：

```powershell
Get-WorkerBandwidth                 # 例: Worker bandwidth: 100% (10 MB/s)
Set-WorkerBandwidth 50              # 0=暂停, 10=1MB/s, 20=2MB/s, 50=5MB/s, 100=10MB/s
Set-WorkerBandwidth 100             # 恢复
```

命令只是往 `<InstallDir>\logs\bandwidth.json` 写一个 `{"percent": <0-100>}`，Worker 每次限速判断时都会重新读它，所以改完立即生效（0 = 暂停）。

查当前生效值：`Get-WorkerBandwidth`，或直接 `download-worker status`（它会显示带宽上限；如果最近 10 分钟有下载，还会附一条从 `logs\worker.log` 算出的实测均速）。带宽文件缺失/损坏时 Worker 按 100% 跑。

### 执行策略（安装时会自动处理）

Windows 客户端的出厂执行策略是 `Restricted` —— 在这个设置下**任何** `.ps1` / `.psm1` 脚本文件都不能被加载，上面这两个命令也就无从注册。安装器的处理方式：

1. 用一个**全新的 PowerShell 进程**真的把命令跑一遍（跑之前先清掉安装器自己可能携带的进程级策略，模拟你新开窗口的环境）；
2. 如果失败原因是执行策略，**问你一次**是否放开当前用户的策略（等价于 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`：用户级、不需要管理员、可逆；**本机创建的脚本可以运行，从网上下载的脚本仍需签名或 `Unblock-File`**）；
3. 同意就设置并重新验证；不同意（或加了 `-SkipExecutionPolicyFix`，或检测到是非交互式环境）就只打印提示，安装继续。

对应的纪律：

* 无人值守安装想直接放开 -> 加 `-FixExecutionPolicy`；不想让它碰策略 -> 加 `-SkipExecutionPolicyFix`；
* 手工等价命令：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`（改完要**新开**窗口才生效）；
* 完全不碰策略的兜底：直接编辑 `<InstallDir>\logs\bandwidth.json`（内容就是一个 `{"percent": 50}`）。

## 手动开关：`download-worker on` / `off` / `status`

安装器会在 `%USERPROFILE%\bin` 放一个 `download-worker.cmd`，并把该目录加入**用户 PATH**（安装器也会顺手更新当前会话的 PATH）。**新开**一个命令行窗口（cmd / PowerShell 都行，**不需要管理员**），直接敲：

```powershell
download-worker status              # 看现在开着还是关着（默认动作）
download-worker off                 # 把这台 PC 从下载池里摘出去：Worker + 隧道都停
download-worker on                  # 放回下载池：Worker + 隧道都回来
download-worker restart             # 重启 Worker + 隧道，但不改变开关状态（等价于 off→on 的进程部分）
download-worker proxy               # 看当前代理设置
download-worker proxy http://<代理IP>:<端口>   # 设 Worker 自己的代理（下个请求即生效）
download-worker proxy socks5h://<代理IP>:<端口>
download-worker proxy off           # 清掉代理，只走直连
download-worker test 268435455      # 实测这台 PC 的下载速度（走配置里的代理）
```

> 代理只进 `worker-config.json` 并立刻被 worker.py 读到，**不需要 restart**；指向本机回环（`127.0.0.1` / `localhost`，也就是私人代理）会被当场拒绝。

### `test` = 在这台 PC 上实测下载速度

```powershell
download-worker test 268435455                          # 拉 256 MiB，走配置里的代理
download-worker test 1G                                 # 也可以用单位写法
download-worker test 268435455 "https://host/path/file" # 换一个目标文件
download-worker test 256M -Direct                       # 绕开代理，测直连做对比
```

和手工那条 `curl.exe --proxy <代理> -L -r 0-<n-1> -o NUL -w "..." <url>` 做的是同一件事：拉 `<bytes>` 字节（**纯数字 = 字节数**，与 curl `-r 0-` 后面那个数同款；也可写 `256M` / `1G` / `512K`）**丢进 NUL 不落盘**，进度表实时打在终端，结束后报告实测速度（MiB/s + MB/s）、耗时、接收字节数、HTTP 状态码，并按该速度推算 1 GiB / 10 GiB 各需多久。

| 项 | 说明 |
|---|---|
| 出口 | 默认走 `proxy.url` 配的那条（worker.py 真正用的那条路）；配置里是回环代理会**拒用并转直连**；`-Direct` 直接绕开代理做对比 |
| 默认目标 | 省略 url 时用内置的公开 SRA 文件（`sra-pub-run-odp.s3.amazonaws.com`，支持 Range、无需凭据） |
| 上限 / 提醒 | 单次样本上限 10 GiB；样本 < 1 MiB 提示读数没意义；实测 < 256 KB/s 提示「比预期慢很多」 |
| 副作用 | **没有**：只读配置、只跑 curl，不改配置、不碰 worker / 隧道 / 计划任务，也不往磁盘写数据 |

测的是**这台 PC 自己的出口**，与 Linux 侧、与其它 PC 无关 —— 用来验证「这台机器直连 vs 走代理哪个快」或排查「怎么变慢了」。

> 没生效多半是窗口没重开（PATH 是进程启动时读的）。等不及就关掉窗口重开，或直接用全路径：
> `<InstallDir>\download-worker.ps1 -Action off`。

### `off` = 整机下线（用户确认的语义）

| 步骤 | 动作 |
|---|---|
| 1 | 创建 `<InstallDir>\paused.flag` —— **权威开关**：Worker 守护与隧道守护看到它就都不再拉起进程 |
| 2 | 停掉本机属于本安装目录的 python / pythonw 进程与 `ssh -R <port>` 隧道进程 |
| 3 | 尽力 `Stop-ScheduledTask` + `Disable-ScheduledTask`（三个任务；失败只警告） |
| 4 | 通过 `ssh <linux_user>@<linux_host>` 执行 `worker_pool.py --set-enabled <port> false`：Linux 池里这个口标记为**人为关闭**，调度器不会再往它派任务，**也不会**因为端口不通而报掉线告警 |

`on` 是反向操作：删 `paused.flag` → 启用并启动计划任务 → 轮询 `/health`（最多 30 秒）→ 同步 `--set-enabled <port> true`。
`status` 打印：开关状态（paused.flag）、**当前带宽上限**（读 `logs\bandwidth.json`；若最近 10 分钟有下载，另附一条实测算得的均速）、**下载出口（直链 / 代理，附各自 IP，见下节）**、三个计划任务状态、本安装目录的进程、`/health`、Linux 池里该口的 enabled/状态。

几点须知：

* **正在下载的任务不会丢**：`off` 相当于拔线，Linux 侧调度器会把它踢出池并重排队，任务带 sidecar 断点，换别的出口或等它回来都能续传。
* **第 4 步是「尽力而为」**：ssh 不通只打印 WARN，不影响本机停机；此时 Linux 池只知道端口不通（会按掉线处理并告警），恢复后自动一致。
* **和带宽命令的区别**：`Set-WorkerBandwidth 0` 只是暂停下载流量（Worker、隧道、池内在线状态都不变，任务仍可派进来排队）；`download-worker off` 是整机退出下载池。临时让路用前者，收工/搬家/维护用后者。
* **Linux 侧等价操作**（不碰 Windows 时也可用）：`python3 ~/script/download-worker/worker_pool.py --set-enabled <端口> false`，或 `--get-enabled <端口>` 查。

### `status` 里的「下载出口」行（直链还是代理、IP 是多少）

`status` 会用三行说明**这次下载走的是哪条路**：

```text
  [ OK ]  下载出口：WORKER_PROXY（本机独立代理）—— <代理IP>:<端口> → ftp.sra.ebi.ac.uk 
          配置代理：WORKER_PROXY http://<代理IP>:<端口>（HTTP）—— 优先走它，失败才回退直连
          私人代理：禁用（下载不占用）
```

| 行 | 来源 | 怎么读 |
|---|---|---|
| 下载出口 | `logs\worker.log` 里最后一条 `[NETWORK] route=` | 最近一次请求**实际**走的出口：`DIRECT` = 直连（附本机出口 IP + 目标主机），`WORKER_PROXY` = Worker 自己的代理（附它实际连的 `host:port`），`WINDOWS_PRIVATE_PROXY` = 本机私人代理（**出现就是异常**，说明配置被改过）。方括号里是那条日志的时间 —— **时间旧 = 那之后就没再下过东西**，不是显示错误。 |
| 配置代理 | `worker-config.json` 的 `proxy.url` | 配置上的代理出口，附带协议（HTTP / SOCKS5）；显示「未配置（只走直连）」= 这台 PC 直连不通就真的没办法了。用 `download-worker proxy <url>` 改。 |
| 私人代理 | `worker-config.json` 的 `proxy.windows_private` | 正常必须是「禁用」；若显示已启用，会附上它只对多大的小文件生效（下载大文件/大小未知一律不走它）。 |

装好后还没跑过下载（或本机 `worker.py` 仍是旧版）时，这一行显示 WARN「日志里没有 [NETWORK] 记录」，不是故障。
直连 / 代理各自的**链路**与切换规则见前面的「代理出口（下载流量走哪条路）」一节。

## HTTP API

Worker 是 FastAPI 应用，默认监听 `http://<Tailscale IP>:8765`；经隧道后从 Linux 侧访问 `http://127.0.0.1:<LinuxTunnelPort>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 存活探测：`{"status":"ok","worker":"local-download-worker"}` |
| `GET` | `/stream?url=<公网 URL>` | **流式代理**：把上游字节流原样转发（`Range` 透传，便于分段 / 断点续传）；上游必须是 http/https 且解析到**公网 IP** |
| `POST` | `/download` | 让 Windows 侧直接落盘：JSON `{"url": "<公网 URL>", "output": "<Windows 路径>"}` |

示例（在 Linux 侧）：

```bash
# 流式取回一个文件
curl -s "http://127.0.0.1:<端口>/stream?url=https://example.org/big.tar" -o big.tar

# 让 Windows 机器自己下载并保存到它的 D 盘
curl -s -X POST http://127.0.0.1:<端口>/download \
     -H "Content-Type: application/json" \
     -d '{"url":"https://example.org/big.tar","output":"D:\\Downloads\\big.tar"}'
```

`/stream` 的目标校验会拒绝解析到私有 / 回环 / 链路本地 / 保留 / 组播地址的 URL（防止把 Worker 当跳板打内网），只允许 `http` / `https`。

## 多台 Windows PC

Linux 侧 `127.0.0.1:<LinuxTunnelPort>` 一个端口只能被一条隧道占用，所以**第二台 PC 必须换端口**：

| PC | `-LinuxTunnelPort` | Linux 侧调用地址 |
|---|---|---|
| 第一台 | <端口>（默认） | `http://127.0.0.1:<端口>` |
| 第二台 | <端口> | `http://127.0.0.1:<端口>` |

端口被占用时 `ExitOnForwardFailure=yes` 会让 ssh 立即退出，安装器会报「隧道进程未运行」并提示换端口。

## 代理出口（下载流量走哪条路）

**原则：下载先走 Worker 自己的代理（WORKER_PROXY，本机直连它，不经隧道）；它连不上才回退直连（DIRECT）；这台 PC 自己的私人代理绝不承载下载数据。**

为什么要有这套东西：`httpx` 默认 `trust_env=True`，会读取 Windows 的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`。装了私人代理（clash 之类）的机器上，**所有** `/stream`、`/download` 流量会悄悄从私人代理出去，白白消耗机主的代理流量池。现在 Worker 建的每个 HTTP 客户端都显式 `trust_env=False`，环境变量代理彻底失效；出口只能由 `worker-config.json` 的 `proxy` 块决定。

**直连自己那个代理**，隧道只保留 `-R`（把 worker 发布给 Linux）这一件事。

| 出口 | 何时用 | 说明 |
|---|---|---|
| `WORKER_PROXY` | **配了就先走它** | `proxy.url` 指的代理，本机直接连（`http://` / `socks5://` / `socks5h://`，后者由代理解析 DNS）。`retries`（默认 2）次尝试，中间隔 1.5 s——一次 DNS 抖动不该把请求推到下一条出口 |
| `DIRECT` | 没配代理时唯一出口；配了则作**兜底** | 直连。代理连不上（网络层错误）时自动回退，下载不会因为代理挂了而停 |
| `WINDOWS_PRIVATE_PROXY` | 默认**禁用**，且大文件永不使用 | 本机私人代理。即使有人把 `windows_private.enabled` 打开，`Content-Length ≥ large_file_threshold_bytes`（默认 1 GiB）**或大小未知**的文件也不会进这条出口 |

**只有网络层错误才换出口**：DNS 解析失败、连接超时、连接被拒、网络不可达（Python 里的 `httpx.TransportError`）。HTTP 404 / 403 / 鉴权失败等**响应**错误原样返回给调用方，不会去试下一个出口、更不会无限换代理。

**私人代理有两道锁**：命令行（`download-worker proxy`、安装器）拒绝任何指向本机回环的目标（`127.x` / `localhost` / `::1`），worker.py 收到同样的值也会忽略并记一条 WARN 日志——两层都不通。

链路长这样：

```text
Linux 侧                    Windows 侧
linux_downloader.py
   │  http://127.0.0.1:<端口>
   ▼
隧道 ssh ──── Tailscale ────► worker.py ──► WORKER_PROXY ──► <代理IP>:<端口> ──► ENA / 目标站
      -R <端口>(worker)            │              （本机直接连，不经隧道）
                                 └─► DIRECT ────► ENA / 目标站（代理不可达时的兜底）
```

**日志**：Worker 每次请求都写一行 `[NETWORK] plan=WORKER_PROXY,DIRECT url=<去 query 的 URL> range=<...>`，实际用哪条出口由结果行给出：

```text
[NETWORK] route=WORKER_PROXY url=https://ftp.sra.ebi.ac.uk/... status=206 range=bytes=0-... proxy=<代理IP>:<端口>
[NETWORK] route=DIRECT       url=... status=206 ... proxy=-
```

日志里的 URL 去掉 query（S3 预签名 URL 的凭据就在 query 里），代理只记 `host:port`，**不记任何凭据**（带用户名口令的 url 也只写 `host:port`）。响应头里也带 `x-worker-route`（值为 `WORKER_PROXY` / `DIRECT` / `WINDOWS_PRIVATE_PROXY`），回调方可直接看到这次走的哪条出口。文件落盘完成的那行（`STREAM complete ... route=...`）同样带出口名。

**开关**：安装时给 `-WorkerProxy <url>`，或不带参数让它问一次；装好后改配置用 `download-worker proxy <url>` / `proxy off`——**下个请求即生效，不必 restart**

## 安全说明

* **不引入任何密钥**：安装包不含 SSH 私钥；隧道用的是目标机自己的 SSH 免密登录。
* **监听面最小**：Worker 只绑定本机 Tailscale 地址，防火墙规则只放行 `100.64.0.0/10`。
* **不出公网**：隧道是 Windows -> Linux 的反向连接，Windows 侧不需要公网 IP、不需要端口映射。
* **SSRF 防护**：`/stream` 只接受能解析到公网 IP 的 http/https 目标；`/download` 由调用方决定落盘路径 —— 也就是说 **Worker 的控制口只应暴露给可信任的 Linux 侧**（它确实可以往 Windows 任意可写路径写文件，这是功能的一部分）。
* **执行策略**：只有在你同意（或显式加 `-FixExecutionPolicy`）时，安装器才会把**当前用户**的策略设为 `RemoteSigned`；它不动机器级策略、不需要管理员。
* **代理凭据**：`proxy.url` 可带用户名口令（`http://user:pass@host:port`），它只写进本机 `worker-config.json` 并用于建连；**日志、`status`、响应头里一律只出现 `host:port`**（回归测试 T9 守着这条）。指向本机回环的代理地址会被命令行与 worker.py **两层**拒绝，这台 PC 的私人代理不会因为误配而承载下载数据。

## 已知限制

Windows MCP Server、Worker Pool、多 Worker 调度、Tailscale 自动安装/登录、自动测速选 Worker、公网 Worker、GUI 配置程序、多用户权限、云端管理 —— 都不在本包范围内。

本包只做一件事：**把一台已经可用形态的 Windows Worker 变成可重复安装的软件包**。

## 可以怎么改（欢迎 Issue / PR）

这个包只解决「我自己的 Windows PC + 我自己的 Linux 服务器」这一个场景，所以刻意做窄了。
下面这些方向**都没做**，也**不打算由作者做** —— 需要的人直接改，欢迎提 Issue / PR：

| 方向 | 现在的样子 | 改造要点 |
|---|---|---|
| **不依赖 Tailscale** | Windows 侧只监听 Tailscale 地址，防火墙也只放行 `100.64.0.0/10` | 隧道的传输其实**与 Tailscale 无关**（`ssh -R` 本来就是主动连出，NAT 后面照样能用）。把「取本机 Tailscale IP」换成普通网卡地址、防火墙规则跟着换，就能在纯 SSH 可达的环境里跑。要改的是两处：`worker/worker_config.py` 的 `is_tailscale_ip()`，和安装器里「取址 + 建规则」那一段 |
| **打包成 exe** | 依赖目标机预装 Python（含 `venv`） | 用 PyInstaller 把 `worker.py` / `start_worker.py` / `start_tunnel.py` 三个入口各打一个 exe，安装器改成拷 exe；记得把 `.venv` 那一步一起去掉 |
| **换掉 SSH 传输层** | 只有 `ssh -R` 一种隧道 | 换成 WireGuard / frp / 自建长连接都行 —— 只要 Linux 侧最终得到一个 `127.0.0.1:<port>`，Worker 侧一行都不用改 |
| **多出口调度** | 池与选口在 Linux 侧（不属于本包） | 本包内可以加「按源站测速并上报」，让调度端有依据可选 |
| **Windows 侧 MCP Server** | 没有；MCP 适配层在 Linux 侧（见 `mcp/`） | 若想让 agent 直接调用 Windows 本机 |

动手前建议先读 `worker/worker_config.py`（所有可变配置都从这里出）和安装器第 5 步的旧版本清理逻辑 ——
这两处是本包里最容易「改了一处、别处不认」的地方。

## 许可

MIT，见 [LICENSE](LICENSE)。
