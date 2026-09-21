# downloader-mcp —— 统一下载 MCP（download-worker 2.x 前端）

本 MCP 是 **Adapter，不是第二个下载器**：只做「参数校验 → 调既有脚本 / 读既有文件 → 结构化返回」，
不实现 Range / 分块 / 重试 / 续传 / 校验 / zero scan —— 那些属于 `tools/linux_downloader.py` 与 Windows 侧 Worker。

## 文件

| 文件 | 说明 |
|---|---|
| `mcp_server.py` | **本体**（纯 stdlib，系统 `python3` 直跑，无需 venv） |
| `用户手册.md` | **给人看的手册**（怎么提交 / 看进度 / 改线数 / 常见问题）—— 想用不想读代码就看它 |

## 安装

MCP 本体要在**工具链已经跑起来**之后才注册：它自己不下载，只是统一入口。

```bash
# 1) 取仓库
git clone <仓库地址> download-worker && cd download-worker

# 2) 生成配置模板，填自己的值（站点地址、路径、身份…）
mkdir -p ~/.config/download-worker
python3 tools/dl_config.py --export-example > ~/.config/download-worker/site.json
$EDITOR ~/.config/download-worker/site.json
python3 tools/dl_config.py --check          # 必填项都填了就该是 0 problems

# 3) 起 driver（长驻；先在 tmux/screen 里跑起来，队列才有东西可派）
python3 tools/dl_all_priority.py

# 4) 注册 MCP 到你的客户端（见下）
```

配置只在两处找：`$DL_SITE_CONFIG`，或 `~/.config/download-worker/site.json`。
**仓库里不带任何站点真值**，所有部署差异都在这一步填。

## 注册

```json
"downloader": {"type": "stdio", "command": "python3",
               "args": ["<本目录>/mcp_server.py"],
               "env": {}}
```

不需要在 `env` 里给任何东西 —— 本体自己认得出工具目录（见下）。
新增的 MCP server 通常**不进已开着的会话**，需要新会话 / 重连才生效。

## 定位方式（为什么和工具目录分开放也能跑）

本体不假设「工具脚本就在我旁边」：

```
WORKER_DIR = $DL_WORKER_DIR
             或 <本体目录>/../tools（存在 dl_req.py 才算数）
             或 ~/download-worker/tools
```

`dl_req.py` / `dl_ctl.py` / `worker_pool.py` / `linux_downloader.py` / `state/` 全部按
`WORKER_DIR` 解析，`sys.path` 也插的它（`import dl_control` 取平台表与等级表）。
**仓库 clone 到哪都行，不用配环境变量**；把本体单独搬走（例如与其它 MCP 统一存放）时
才需要给 `DL_WORKER_DIR`。driver 名用 `DRIVER_NAME` 覆盖（决定读哪份
`state/driver_<name>.json`）。

工具目录认不出来时启动会在 **stderr** 上说明（stdio MCP 的 stdout 只放 JSON-RPC），
只警告不退出 —— 只读工具在没有工具目录时仍有意义。

台账与告警的**默认路径不写死在本文件里**（本仓库是公开的），依次取：
环境变量 → 配置文件 → 空。配置文件就是 `linux/dw_tasks.py` 那份
（`~/.config/download-worker/dw_tasks.json`，`DW_TASKS_CONFIG` 换位置）：

| 用途 | 环境变量 | 配置键（`dw_tasks.json`） |
|---|---|---|
| 下载台账 | `DL_STATUS_CSV` | `status_csv_candidates`（取第一条） |
| 告警流水 | `DL_ALERTS_FILE` | `alerts_file` |

留空时只有 `alerts` / `overview` 两个工具会提示该怎么配，其余工具照常。

## 工具（10 个）

`submit`（提交到统一队列，带下载等级 p0–p4；一次多条用 `items`）/ `queue_status`（含**按通道**的逐线进度）/
`ctl`（线数·暂停·插队·让路）/ `lease_status` / `worker_status` / `alerts` / `overview` /
`inventory`（清单自管刷新；需另行配置刷新器，未配时该工具会说明）/ `download_status` /
`download`（临时单文件，租约感知）。

**无状态**：状态全在共享文件与 flock 里 ⇒ 多 agent 各起一份 MCP 进程也不会互相抢口
（口租约由内核 flock 仲裁，进程死了自动释放）。
队列 / 等级 / 排队口径见 [`用户手册.md`](用户手册.md)。
