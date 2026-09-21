<#
  download-worker - manual on/off switch for this PC's download outlet.

  Usage (any terminal, after running the installer):
      download-worker              # = download-worker status
      download-worker status       # tasks / processes / health / Linux-side queue
      download-worker process      # live view of the Linux-side downloads (Ctrl+C exits)
      download-worker off          # take this PC out of the download pool (整机下线)
      download-worker on           # put it back
      download-worker restart      # restart worker + tunnel, keep the on/off state
      download-worker proxy                     # show the download exit (代理 vs 直连)
      download-worker proxy http://ip:port      # send downloads through this proxy
      download-worker proxy socks5h://ip:port   # ... SOCKS5, proxy resolves DNS
      download-worker proxy off                 # clear it (直连 only)
      download-worker test <bytes> [url]        # measure the real download speed here
      download-worker test 256M -Direct         # ... sample the direct route instead

  "test" pulls that many bytes (268435455, or 256M / 1G) of a public file with
  curl.exe and reports the measured speed, the same measurement as
  `curl.exe --proxy <proxy> -L -r 0-268435455 -o NUL -w "..." <url>`.
  By default it goes through the configured proxy; -Direct bypasses it.  Only
  the exit of THIS PC is measured - nothing is written to disk.

  The proxy above is a proxy OF ITS OWN, reached directly over the network; it
  takes effect on the next request (worker.py re-reads the config every time).
  This PC's *private* proxy is never a download exit: a loopback address such as
  127.0.0.1:7897 is refused here, and worker.py refuses it again on its side.

  What "off" does, in order:
      1. creates <InstallDir>\paused.flag - while it exists both supervisors
         (worker_supervisor.py / tunnel_supervisor.py) refuse to start anything.
         This is the authoritative switch and needs no administrator rights.
      2. stops the running supervisor / worker / ssh processes of THIS installation
         (matched by this install path, never anything else on the machine).
      3. best effort: disables the two scheduled tasks - needs an elevated shell;
         without it the machine still stays offline thanks to step 1.
      4. best effort: tells the Linux side to set workers.json -> enabled:false for
         this PC's port, so the monitoring there does not raise a false
         "worker down" alert; it is re-enabled by "on".
  "on" reverses all four.  Steps 1-3 need no Internet; only step 4 uses ssh.
#>

#Requires -Version 5.1

param(
    [Parameter(Position = 0)]
    [ValidateSet("status", "on", "off", "restart", "proxy", "test", "process")]
    [string]$Action = "status",

    # "proxy": the proxy url to set, or "off"/"none" to clear it (omit = show it).
    # "test" : how many bytes to sample - a plain byte count ("268435455", the same
    #          number you would put after curl's -r 0-) or with a unit ("256M", "1G").
    [Parameter(Position = 1)]
    [Alias("ProxyUrl")]
    [string]$Arg1,

    # "test": the http(s) url to sample (omit = the built-in public test file).
    [Parameter(Position = 2)]
    [string]$Arg2,

    # "test": sample the DIRECT route even when a proxy is configured.
    [switch]$Direct,

    # "process": seconds between two live frames (default 2).
    [int]$ProcessInterval = 2
)

$ErrorActionPreference = "Stop"

# Version 1.0 on purpose: the job here is to switch the machine on/off reliably, and a
# config written by an older installer has no linux_worker_pool_path.  Version 2.0 would
# turn that missing JSON key into a hard error; 1.0 still catches variable typos but reads
# an absent property as $null (the code below treats that as "use the default").
Set-StrictMode -Version 1.0

$InstallDir      = $PSScriptRoot
$ConfigPath      = Join-Path $InstallDir "worker-config.json"
$PauseFlag       = Join-Path $InstallDir "paused.flag"
$TunnelLog       = Join-Path $InstallDir "logs\tunnel_supervisor.log"
$WorkerTask      = "Local Download Worker"
$TunnelTask      = "Local Download Tunnel"
$SshExe          = "C:\Windows\System32\OpenSSH\ssh.exe"
$CurlExe         = Join-Path $env:SystemRoot "System32\curl.exe"
# Sampled by "download-worker test" when no url is given.  A public, range-capable
# test file with no credentials attached -- anything comparable will do; override
# it per run by passing a url to "download-worker test".
$DefaultTestUrl  = "https://proof.ovh.net/files/10Mb.dat"
$DefaultPoolPath = "~/download-worker/tools/worker_pool.py"   # ~ is expanded by the Linux login shell
$DefaultTasksPath = "~/download-worker/linux/dw_tasks.py"  # ~ is expanded by the Linux login shell
# Two layers of timeout guard every ssh that a person is waiting on (see Get-SshKeepAliveArgs).
# A normal remote call takes a few seconds; anything past this is stuck, not slow.
$RemoteTimeoutSec = 20
$PoolMarker       = "__POOL_SNAPSHOT__"

function Write-Head { param([string]$Text) Write-Host ""; Write-Host ("== " + $Text) -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host ("  [ OK ]  " + $Text) -ForegroundColor Green }
function Write-Warn2{ param([string]$Text) Write-Host ("  [WARN]  " + $Text) -ForegroundColor Yellow }
function Write-Info { param([string]$Text) Write-Host ("          " + $Text) }

function Get-Cfg {
    if (-not (Test-Path $ConfigPath)) { return $null }
    try { return (Get-Content -Path $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { return $null }
}

function Get-Port {
    # 隧道口：Linux 端 127.0.0.1:<port> 的反向转发端口（不是 Windows 上的监听口）
    $cfg = Get-Cfg
    if ($null -ne $cfg -and $null -ne $cfg.linux_tunnel_port) { return [int]$cfg.linux_tunnel_port }
    return 8766
}

function Get-WorkerPort {
    $cfg = Get-Cfg
    if ($null -ne $cfg -and $null -ne $cfg.worker_port) { return [int]$cfg.worker_port }
    return 8765
}

function Get-WorkerHosts {
    # Worker 绑的是本机 Tailscale IPv4（start_worker.py 的设计），不是 127.0.0.1。
    # 依次尝试：配置里的 worker_host → 本机 Tailscale IP → 127.0.0.1（兜底）。
    $cfg = Get-Cfg
    $list = New-Object System.Collections.ArrayList

    if ($null -ne $cfg -and $cfg.worker_host) {
        $h = "$($cfg.worker_host)".Trim()
        if ($h -and $h -notin @("auto", "detect")) { [void]$list.Add($h) }
    }

    try {
        $tsExe = "C:\Program Files\Tailscale\tailscale.exe"
        if (Test-Path $tsExe) {
            foreach ($line in @(& $tsExe ip -4 2>$null)) {
                $line = "$line".Trim()
                if ($line -match "^100\.") { [void]$list.Add($line) }
            }
        }
    }
    catch { }

    [void]$list.Add("127.0.0.1")
    return @($list | Select-Object -Unique)
}

function Get-InstallProcess {
    # Only processes whose command line points into THIS install directory; nothing else
    # on the machine is ever touched.
    param([string]$Pattern)
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like ("*" + $InstallDir + "*") -and
            $_.CommandLine -like ("*" + $Pattern + "*")
        })
}

function Test-Health {
    # Plain HttpWebRequest with the proxy disabled: Invoke-WebRequest would honour a
    # system proxy, which must never sit between this script and the Worker.
    #
    # Fix: this used to probe 127.0.0.1:<tunnel port> - a port
    # that only exists on the LINUX side (reverse forward).  Windows listens on
    # the Worker port and on the Tailscale address, so the check reported a bogus
    # WARN on PC2 while the Worker was healthy.  Probe the Worker's real address.
    param([int]$TimeoutSeconds = 5)

    foreach ($h in Get-WorkerHosts) {
        try {
            $request = [System.Net.HttpWebRequest]::Create(("http://{0}:{1}/health" -f $h, (Get-WorkerPort)))
            $request.Proxy = $null
            $request.Timeout = $TimeoutSeconds * 1000
            $response = $request.GetResponse()
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
            $body = $reader.ReadToEnd()
            $reader.Close()
            $response.Close()
            return $body
        }
        catch {
            continue
        }
    }

    return $null
}

function Get-BandwidthPercent {
    # logs\bandwidth.json is what Set-WorkerBandwidth writes and what the Worker
    # re-reads on every throttle decision.  `status` has to show it: a PC limited to
    # 0% (paused) or 10% otherwise looks exactly like a broken one (user request).
    # Missing / unreadable file = the Worker runs at 100% (worker.py
    # DEFAULT_BANDWIDTH_PERCENT), so callers treat $null as "100%, unknown".
    $path = Join-Path $InstallDir "logs\bandwidth.json"

    if (-not (Test-Path $path)) { return $null }

    try {
        $data = Get-Content -Path $path -Raw -Encoding UTF8 | ConvertFrom-Json
        return [int]$data.percent
    }
    catch {
        return $null
    }
}

function Get-RecentThroughput {
    # Best effort, never an error: the Worker logs every finished 256 MB chunk as
    #   TIMESTAMP | INFO | STREAM complete host=... status=206 bytes=<n> elapsed=<f>s
    # Average the chunks finished within the last 10 minutes -> "how fast is this PC
    # actually going right now".  Nothing recent / unparseable -> no line at all.
    $log = Join-Path $InstallDir "logs\worker.log"

    if (-not (Test-Path $log)) { return $null }

    try {
        $cutoff = (Get-Date).AddMinutes(-10)
        $bytes = 0.0
        $seconds = 0.0
        $chunks = 0

        foreach ($line in @(Get-Content -Path $log -Tail 200 -ErrorAction Stop)) {
            if ($line -notmatch '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| \w+ \| STREAM complete .*bytes=(\d+) elapsed=([0-9.]+)s') { continue }
            if ([datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null) -lt $cutoff) { continue }

            $bytes += [double]$Matches[2]
            $seconds += [double]$Matches[3]
            $chunks++
        }

        if ($chunks -eq 0 -or $seconds -le 0) { return $null }

        return ("最近 10 分钟完成 {0} 块，实测均速 ≈ {1:N1} MiB/s" -f $chunks, ($bytes / $seconds / 1MB))
    }
    catch {
        return $null
    }
}

function Get-LocalOutboundIp {
    # This PC's own source address for anything leaving it (the default-route
    # interface) - i.e. the address a DIRECT download goes out from.  Pure NetTCPIP
    # cmdlets: no packet is sent, so it still answers while the campus network is
    # broken.  $null means unknown and callers must not fail on it.
    try {
        $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction Stop |
            Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
        if ($null -eq $route) { return $null }

        $addr = Get-NetIPAddress -InterfaceIndex $route.ifIndex -AddressFamily IPv4 -ErrorAction Stop |
            Select-Object -First 1
        if ($null -ne $addr) { return $addr.IPAddress }
    }
    catch { }

    return $null
}

function Get-LastRoute {
    # The exit the Worker used for the most recent request, parsed from worker.log:
    #   TIMESTAMP | INFO | [NETWORK] route=DIRECT url=... host=... status=206 range=... proxy=-
    # `proxy` is the endpoint that actually carried it: "-" for DIRECT, host:port for
    # WORKER_PROXY (the proxy configured for this Worker) or the private proxy.
    # The route-failure line (route=X attempt=... error=...) does not match on purpose -
    # it says nothing about which exit served anything.  $null = no record at all
    # (fresh install, or a worker.py from before the route layer).  Never an error.
    $log = Join-Path $InstallDir "logs\worker.log"

    if (-not (Test-Path $log)) { return $null }

    try {
        $lines = @(Get-Content -Path $log -Tail 400 -ErrorAction Stop)
    }
    catch {
        return $null
    }

    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        $line = "$($lines[$i])"
        if ($line -notmatch '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| \w+ \| \[NETWORK\] route=(\S+) url=(\S+) host=(\S+) status=(\d+) range=(\S+) proxy=(\S+)$') { continue }

        # $Matches[0] is the whole line, so the captures start at [1]: 7 groups -> [7]
        return [pscustomobject]@{
            Time   = $Matches[1]
            Route  = $Matches[2]
            Url    = $Matches[3]
            Host   = $Matches[4]
            Status = $Matches[5]
            Range  = $Matches[6]
            Proxy  = $Matches[7]
        }
    }

    return $null
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Sync-LinuxRegistry {
    param([bool]$Enabled)

    $cfg = Get-Cfg
    if ($null -eq $cfg -or -not $cfg.linux_user -or -not $cfg.linux_host) {
        Write-Warn2 "linux_user/linux_host 未配置，跳过 Linux 侧同步"
        return
    }

    $port = [int]$cfg.linux_tunnel_port
    if ($port -le 0) {                      # unreadable config: never write a bogus port into the pool
        Write-Warn2 "worker-config.json 里没有可用的 linux_tunnel_port，跳过 Linux 侧同步"
        return
    }

    $poolPath = "$($cfg.linux_worker_pool_path)"
    if (-not $poolPath) { $poolPath = $DefaultPoolPath }
    $flag = if ($Enabled) { "true" } else { "false" }

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $SshExe -o BatchMode=yes -o ConnectTimeout=10 ("{0}@{1}" -f $cfg.linux_user, $cfg.linux_host) `
            ("python3 {0} --set-enabled {1} {2}" -f $poolPath, $port, $flag) 2>&1
        $code = $LASTEXITCODE
    }
    catch {
        $output = "$_"
        $code = -1
    }
    finally {
        $ErrorActionPreference = $previous
    }

    if ($code -eq 0) {
        Write-Ok ("Linux 侧已同步：口 {0} enabled={1}" -f $port, $flag)
        foreach ($line in @($output)) { if ("$line".Trim()) { Write-Info "$line" } }
    }
    else {
        Write-Warn2 ("Linux 侧同步失败（不影响本机开关）：{0}" -f ("$output").Trim())
        Write-Info  ("可稍后在 Linux 上手动执行：python3 {0} --set-enabled {1} {2}" -f $poolPath, $port, $flag)
    }
}

function Get-TasksPath {
    # Linux 侧只读任务视图（dw_tasks.py）。和 linux_worker_pool_path 一样：旧配置没有
    # 这个键就回默认值（~ 由 Linux 登录 shell 展开）。
    $cfg = Get-Cfg
    if ($null -ne $cfg -and $null -ne $cfg.linux_dw_tasks_path) {
        $path = "$($cfg.linux_dw_tasks_path)".Trim()
        if ($path) { return $path }
    }
    return $DefaultTasksPath
}

function Get-SshKeepAliveArgs {
    # 同一条 ssh 上的两层超时，分工不同、缺一不可：
    #   · ServerAliveInterval / ServerAliveCountMax —— **客户端保活**：15 s 发一次，连续 3 次
    #     没应答（≈45 s）就断开连接。治的是「连接半死」：对端 sshd 或链路已经没了却不发 FIN。
    #   · 远端命令套 `timeout` —— 治的是「命令卡住」。**保活治不了这一种**：命令卡住时 sshd
    #     在传输层照常应答保活，客户端只会一直等下去。必须由远端自己限时。
    return @("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3")
}

function Invoke-LinuxSsh {
    # 跑一条 ssh（带保活选项），stderr 并进输出；返回 @{ Output = <行数组>; Code = <退出码> }。
    # 原生程序写 stderr 不该中断脚本（和 Sync-LinuxRegistry 同一套处理）。
    param([string]$Target, [string]$Command)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $sshArgs = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=10") + (Get-SshKeepAliveArgs) +
                   @($Target, $Command)
        $output = & $SshExe @sshArgs 2>&1
        $code = $LASTEXITCODE
    }
    catch {
        $output = "$_"
        $code = -1
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return @{ Output = @($output); Code = $code }
}

function Get-PoolPath {
    # Linux 侧 worker 池探活脚本。和 linux_dw_tasks_path 一样：旧配置没这个键就回默认值。
    $cfg = Get-Cfg
    if ($null -ne $cfg -and $null -ne $cfg.linux_worker_pool_path) {
        $path = "$($cfg.linux_worker_pool_path)".Trim()
        if ($path) { return $path }
    }
    return $DefaultPoolPath
}

function Get-WorkerLabel {
    # 本机自报名。旧配置没有 worker_label ⇒ 回退到 COMPUTERNAME，已装的机器不必重跑安装器。
    $cfg = Get-Cfg
    if ($null -ne $cfg -and $null -ne $cfg.worker_label) {
        $label = "$($cfg.worker_label)".Trim()
        if ($label) { return $label }
    }
    if ($env:COMPUTERNAME) { return $env:COMPUTERNAME }
    return "(未命名)"
}

function Invoke-LinuxView {
    # 一条 ssh 取回 Linux 侧的只读视图，返回 @{ Frame = <任务帧或 $null>; Pool = <池快照或 $null> }。
    #
    # 为什么两块并成**一条** ssh：两块都要网络往返，分两条会让 status 慢一倍（实测单条 3.6–5.5 s）。
    # -WithPool 是给 status 加的；process 视图只要任务帧，不带这一块。
    param([int]$Limit = 5, [string]$Channel = "", [switch]$WithPool, [switch]$Quiet)

    $cfg = Get-Cfg
    if ($null -eq $cfg -or -not $cfg.linux_user -or -not $cfg.linux_host) {
        if (-not $Quiet) { Write-Warn2 "linux_user/linux_host 未配置，读不到 Linux 侧任务" }
        return @{ Frame = $null; Pool = $null }
    }

    $taskScript = Get-TasksPath
    $command = "timeout {0} python3 {1} --json --limit {2}" -f $RemoteTimeoutSec, $taskScript, $Limit
    # 通道过滤：老 Linux 脚本不认 --channel（会直接 argparse 报错、这块变"没输出"），
    # 所以部署顺序是「先更新 Linux 侧 dw_tasks.py，再换 PC 上的这个脚本」。
    if ($Channel) { $command = "{0} --channel {1}" -f $command, $Channel }
    if ($WithPool) {
        # 两段输出用一行标记隔开；任务那段超时了也照样跑池那段（用 `;` 不是 `&&`）。
        $command = "{0}; echo '{1}'; timeout {2} python3 {3} --json" -f `
            $command, $PoolMarker, $RemoteTimeoutSec, (Get-PoolPath)
    }
    $target = "{0}@{1}" -f $cfg.linux_user, $cfg.linux_host
    $run    = Invoke-LinuxSsh -Target $target -Command $command
    $lines  = @($run.Output)

    # ssh 自己的报错行混在 stderr 里；JSON 一定是唯一以 { 开头的那行。
    $splitAt = -1
    if ($WithPool) {
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ("$($lines[$i])".Trim() -eq $PoolMarker) { $splitAt = $i; break }
        }
    }
    $taskLines = @()
    $poolLines = @()
    if ($splitAt -lt 0) {
        $taskLines = $lines
    }
    else {
        # 注意 PowerShell 的 $a[0..-1] 是**倒序取两个元素**、不是空 —— 两头都要显式判边界。
        if ($splitAt -gt 0) { $taskLines = @($lines[0..($splitAt - 1)]) }
        if ($splitAt + 1 -le $lines.Count - 1) { $poolLines = @($lines[($splitAt + 1)..($lines.Count - 1)]) }
    }

    $frame = $null
    $text = @($taskLines) | Where-Object { "$_".TrimStart() -like "{*" } | Select-Object -First 1
    if (-not $text) {
        if (-not $Quiet) {
            if ($run.Code -eq 124) {
                Write-Warn2 ("Linux 侧任务脚本超过 {0}s 没返回，已放弃（远端卡住，不是本机问题）" -f $RemoteTimeoutSec)
            }
            else {
                Write-Warn2 ("Linux 侧任务脚本没有输出（exit {0}）：{1}" -f $run.Code, ("$($run.Output)").Trim())
            }
            Write-Info  ("  手动确认：ssh {0} `"python3 {1} --text`"" -f $target, $taskScript)
        }
    }
    else {
        try { $frame = $text | ConvertFrom-Json }
        catch { if (-not $Quiet) { Write-Warn2 ("Linux 侧任务输出解析失败：{0}" -f $_) } }
    }

    $pool = $null
    if ($WithPool -and $poolLines.Count -gt 0) {
        $poolText = @($poolLines) | Where-Object { "$_".TrimStart() -like "{*" } | Select-Object -First 1
        if ($poolText) {
            try { $pool = $poolText | ConvertFrom-Json }
            catch { if (-not $Quiet) { Write-Warn2 ("Linux 侧池快照解析失败：{0}" -f $_) } }
        }
    }

    return @{ Frame = $frame; Pool = $pool }
}

function Get-LinuxTasks {
    # 只要任务帧（process 视图与其它调用沿用这个签名）。
    param([int]$Limit = 5, [string]$Channel = "", [switch]$Quiet)
    return (Invoke-LinuxView -Limit $Limit -Channel $Channel -Quiet:$Quiet).Frame
}

function Format-TaskSize {
    # 台账里的 size_gb 可能是空、0 或非数字 —— 一律渲染成等宽文本，不要抛异常。
    param($Entry)
    $value = 0.0
    if ($null -ne $Entry -and $null -ne $Entry.size_gb) {
        [void][double]::TryParse("$($Entry.size_gb)", [System.Globalization.NumberStyles]::Float,
            [System.Globalization.CultureInfo]::InvariantCulture, [ref]$value)
    }
    if ($value -gt 0) { return ("{0,7:N1} GB" -f $value) }
    return (" " * 11)
}

function Get-TasksLines {
    # 一帧 Linux 侧任务 → 一组 {Text, Color} 行。
    # status 逐行打印（带颜色），process 原地重绘（拼等宽文本）。
    # MaxActive：在下任务最多列几条。台账里 DOWNLOADING 常挂着几十条中断残留，
    # 全列会把终端刷满、process 的面板更会被窗口高度截掉 —— 真的在传的排在最前，
    # 够看就行，剩下的用一行汇总交代。
    param($Frame, [int]$Recent = 5, [int]$MaxActive = 8)

    $out = New-Object System.Collections.ArrayList
    $indent = "          "                       # 与 Write-Info 同缩进

    if ($null -eq $Frame) {
        [void]$out.Add(@{ Text = ($indent + "Linux 侧任务：取不到（ssh 没通，或 dw_tasks.py 不在默认路径）"); Color = "Yellow" })
        return $out
    }
    if ($Frame.ok -eq $false) {
        [void]$out.Add(@{ Text = ($indent + ("Linux 侧任务：" + $Frame.error)); Color = "Yellow" })
        if ("$($Frame.hint)") {
            [void]$out.Add(@{ Text = ($indent + "  " + $Frame.hint); Color = "" })
        }
        return $out
    }

    $counts = @()
    if ($null -ne $Frame.counts) {
        foreach ($prop in $Frame.counts.PSObject.Properties) { $counts += ("{0}={1}" -f $prop.Name, $prop.Value) }
    }
    [void]$out.Add(@{ Text = ("  [ OK ]  " + ("Linux 侧任务台账：共 {0} 条   {1}" -f $Frame.total, ($counts -join "  "))); Color = "Green" })
    [void]$out.Add(@{ Text = ($indent + ("台账：{0}   生成：{1} @ {2}" -f $Frame.csv, $Frame.generated, $Frame.host)); Color = "" })

    $active = @($Frame.active | Where-Object { $null -ne $_ })
    $chan = "$($Frame.channel)"
    if ($chan) {
        # 本机视图：$active 已被 Linux 侧过滤成「本机隧道口在下的」。
        # 全局条数照报 —— 否则一眼看去会误以为全局只剩这一条。
        $mine = "本机 {0}：在下 {1} 个（其中正在传输 {2} 个）" -f $chan, $active.Count, $Frame.active_live
        $all  = "全局在下 {0} 个（其中正在传输 {1} 个）" -f $Frame.active_total, $Frame.active_live_total
        [void]$out.Add(@{ Text = ($indent + $mine + "；" + $all + "："); Color = "" })
    }
    else {
        [void]$out.Add(@{ Text = ($indent + ("在下 {0} 个（其中正在传输 {1} 个）：" -f $active.Count, $Frame.active_live)); Color = "" })
    }
    if ($active.Count -eq 0) {
        if ($chan) {
            [void]$out.Add(@{ Text = ($indent + "  （本机此刻没有在下的任务 —— 空闲；新任务由 Linux 侧派给空闲的口）"); Color = "DarkGray" })
        }
        else {
            [void]$out.Add(@{ Text = ($indent + "  （没有 DOWNLOADING / REPAIRING 的任务）"); Color = "DarkGray" })
        }
    }
    $shown = $active
    if ($MaxActive -gt 0 -and $active.Count -gt $MaxActive) { $shown = $active[0..($MaxActive - 1)] }
    foreach ($task in $shown) {
        $percent = "  ?  "
        if ($null -ne $task.percent) { $percent = ("{0,5:N1}%" -f [double]$task.percent) }
        $speed = ""
        if ($null -ne $task.speed_bps -and [double]$task.speed_bps -gt 0) {
            $speed = ("{0,7:N2} MiB/s" -f ([double]$task.speed_bps / 1MB))
        }
        $mark = "  "
        if ($task.live) { $mark = "> " }        # > = sidecar 还新鲜，此刻真在传输
        $itemName = "$($task.name)"
        if ($itemName.Length -gt 34) { $itemName = $itemName.Substring(0, 34) }
        $color = ""
        if (-not $task.live) { $color = "DarkGray" }   # 灰 = 台账里挂着但没在动（中断残留）
        [void]$out.Add(@{ Text = ($indent + ("{0}{1}  {2}  {3,-34} {4}{5}" -f `
            $mark, $percent, (Format-TaskSize $task), $itemName, $task.status, $speed)); Color = $color })
        if ("$($task.file)") {
            [void]$out.Add(@{ Text = ($indent + "       " + $task.file); Color = "DarkGray" })
        }
    }

    if ($shown.Count -lt $active.Count) {
        [void]$out.Add(@{ Text = ($indent + ("  … 另有 {0} 条在下（当前没有传输，多为待重排队的残留）" -f ($active.Count - $shown.Count))); Color = "DarkGray" })
    }

    # 变量名必须叫 $recentList 之类 —— PowerShell 变量名**大小写不敏感**，
    # 写成 $recent 就是上面那个 [int]$Recent 参数本身，把数组赋给 [int] 会当场抛
    # ConvertToFinalInvalidCastException（$ErrorActionPreference=Stop 下直接打死 status）。
    $recentList = @($Frame.recent | Where-Object { $null -ne $_ })
    if ($chan) {
        # 完成记录**分不了 PC**：台账 CSV 里没有"哪个口下的"这一列（文件下完就没有归属了）。
        # 如实标注成全局，别让人误读成"本机刚下完这些"。
        [void]$out.Add(@{ Text = ($indent + ("最近完成 {0} 条（全局；台账不带 PC 归属）：" -f $recentList.Count)); Color = "" })
    }
    else {
        [void]$out.Add(@{ Text = ($indent + ("最近完成 {0} 条：" -f $recentList.Count)); Color = "" })
    }
    if ($recentList.Count -eq 0) {
        [void]$out.Add(@{ Text = ($indent + "  （台账里还没有 DONE / REPAIRED 的记录）"); Color = "DarkGray" })
    }
    foreach ($task in $recentList) {
        $itemName = "$($task.name)"
        if ($itemName.Length -gt 34) { $itemName = $itemName.Substring(0, 34) }
        [void]$out.Add(@{ Text = ($indent + ("  {0}  {1,-34} {2}  {3}" -f `
            (Format-TaskSize $task), $itemName, $task.status, $task.updated)); Color = "" })
    }

    return $out
}

function Show-TasksBlock {
    # status 用：把一帧渲染到终端（带颜色）。
    param($Frame, [int]$Recent = 5, [int]$MaxActive = 8)
    foreach ($line in (Get-TasksLines -Frame $Frame -Recent $Recent -MaxActive $MaxActive)) {
        if ("$($line.Color)") { Write-Host $line.Text -ForegroundColor $line.Color }
        else { Write-Host $line.Text }
    }
}

function Write-Participation {
    # 「这台 PC 此刻在不在给下载供流量」。**Windows 侧自己并不掌握这件事** —— 派活是 Linux 侧
    # driver 决定的（借/还 + 探活），PC 只知道自己有没有正在传的请求；所以唯一出处是 Linux 侧
    # worker_pool.py 的探活快照。traffic 是**探测型**标签：内核 socket 字节计数有增量 ⇒ true。
    # **空闲时也要显式打印**（"无任务"），不能静默 —— 静默正是当初两台 PC 看起来一样的原因。
    param($Pool, [int]$Port)

    if ($null -eq $Pool) {
        Write-Warn2 "下载参与度：读不到（Linux 侧 worker_pool.py 没有输出）"
        return
    }

    $entry = $null
    if ($null -ne $Pool.ports) {
        foreach ($prop in $Pool.ports.PSObject.Properties) {
            if ($prop.Name -eq "$Port") { $entry = $prop.Value; break }
        }
    }
    if ($null -eq $entry) {
        Write-Warn2 ("下载参与度：Linux 侧注册表里没有口 {0}（隧道还没建起来？）" -f $Port)
        return
    }

    if ("$($entry.ok)" -ne "True") {
        Write-Warn2 ("下载参与度：口 {0} 探活失败（{1}）" -f $Port, "$($entry.error)")
        return
    }
    if ($entry.traffic) {
        Write-Ok ("下载参与度：有流量（{0:N2} MB/s，{1} 条连接）" -f `
            [double]$entry.traffic_mbps, [int]$entry.traffic_conns)
    }
    else {
        Write-Info ("下载参与度：无任务（traffic=false）—— 口 {0} 在线，Linux 侧暂时没派活" -f $Port)
    }
}

function Show-Status {
    $cfg = Get-Cfg
    $port = Get-Port

    Write-Head "download-worker 状态（$InstallDir）"
    # 本机身份：两台 PC 的 status 原先除这一带以外逐字节相同，分不清"我在看哪台"。
    $hosts  = @(Get-WorkerHosts)
    $myAddr = if ($hosts.Count -gt 0) { "$($hosts[0])" } else { "(地址未取到)" }
    Write-Info ("本机：{0}（隧道口(Linux) {1} / Worker(本机) {2} / {3}）" -f `
        (Get-WorkerLabel), $port, (Get-WorkerPort), $myAddr)

    if (Test-Path $PauseFlag) {
        Write-Warn2 "已暂停（paused.flag 存在）——本机不会下载，也不会自动恢复"
    }
    else {
        Write-Ok "未暂停（正常参与下载）"
    }

    $percent = Get-BandwidthPercent
    if ($null -eq $percent) {
        Write-Warn2 "带宽：读不到 logs\bandwidth.json（Worker 按 100% 跑）"
    }
    elseif ($percent -eq 0) {
        Write-Warn2 "带宽：PAUSED（0 MB/s）——恢复用 Set-WorkerBandwidth 100"
    }
    else {
        Write-Ok ("带宽：{0}%（≈ {1} MB/s）" -f $percent, ($percent / 10))
    }

    $throughput = Get-RecentThroughput
    if ($throughput) { Write-Info ("实测：" + $throughput) }

    # 下载出口（代理 vs 直链）：先报"最近一次实际走的路"，再报"配置上有哪些路可选"。
    $last = Get-LastRoute
    $localIp = Get-LocalOutboundIp

    if ($null -eq $last) {
        Write-Warn2 "下载出口：日志里没有 [NETWORK] 记录（还没跑过下载，或本机 worker.py 仍是旧版）"
    }
    elseif ($last.Route -eq "DIRECT") {
        $who = if ($localIp) { "本机 " + $localIp } else { "本机 IP 未取到" }
        Write-Ok ("下载出口：DIRECT（直连）—— {0} → {1}   [{2} 最近一次]" -f $who, $last.Host, $last.Time)
    }
    elseif ($last.Route -eq "WORKER_PROXY") {
        Write-Ok ("下载出口：WORKER_PROXY（本机独立代理）—— {0} → {1}   [{2} 最近一次]" -f `
            $last.Proxy, $last.Host, $last.Time)
    }
    elseif ($last.Route -eq "WINDOWS_PRIVATE_PROXY") {
        Write-Warn2 ("下载出口：WINDOWS_PRIVATE_PROXY（本机私人代理 {0}）—— 下载不该走它，请检查配置   [{1} 最近一次]" -f `
            $last.Proxy, $last.Time)
    }
    else {
        Write-Info ("下载出口：{0}（{1}）   [{2} 最近一次]" -f $last.Route, $last.Proxy, $last.Time)
    }

    if ($null -ne $cfg) {
        $proxyUrl = ""
        if ($null -ne $cfg.proxy) { $proxyUrl = "$($cfg.proxy.url)".Trim() }
        if ($proxyUrl) {
            Write-Info ("配置代理：WORKER_PROXY {0}（{1}）—— 优先走它，失败才回退直连" -f $proxyUrl, (Get-ProxyKind $proxyUrl))
        }
        else {
            Write-Info "配置代理：未配置（只走直连 DIRECT）—— 设置用 download-worker proxy <url>"
        }

        $private = $cfg.proxy.windows_private
        if ($null -ne $private -and $private.enabled) {
            # missing threshold = worker.py's own default (1 GiB)
            $threshold = 1073741824
            if ($null -ne $cfg.proxy.large_file_threshold_bytes) { $threshold = $cfg.proxy.large_file_threshold_bytes }
            Write-Warn2 ("私人代理：已启用 {0}:{1} —— 只对小于 {2:N0} MiB 的文件生效，下载大文件永不使用" -f `
                $private.host, $private.port, ($threshold / 1MB))
        }
        else {
            Write-Info "私人代理：禁用（下载不占用）"
        }
    }

    if ($null -ne $cfg) {
        Write-Info ("Linux: {0}@{1}   隧道口(Linux): {2}   Worker 口(本机): {3}" -f `
            $cfg.linux_user, $cfg.linux_host, $cfg.linux_tunnel_port, $cfg.worker_port)
    }
    else {
        Write-Warn2 "读不到 worker-config.json（安装是否完整？）"
    }

    Write-Host ""
    foreach ($name in @($WorkerTask, $TunnelTask)) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Warn2 ("计划任务 {0}：不存在" -f $name)
        }
        else {
            Write-Info ("计划任务 {0}：State={1}, Enabled={2}" -f $name, $task.State, $task.Settings.Enabled)
        }
    }

    Write-Host ""
    $sups = @(Get-InstallProcess -Pattern "_supervisor.py")
    $ssh  = @(Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -like ("*-R " + $port + ":*") })

    if ($sups.Count -gt 0) { Write-Ok ("supervisor 进程：{0} 个" -f $sups.Count) }
    else { Write-Warn2 "supervisor 进程：0 个（暂停状态下属正常；否则请 download-worker on）" }

    if ($ssh.Count -gt 0) { Write-Ok ("ssh 反向隧道进程：{0} 个（-R {1}）" -f $ssh.Count, $port) }
    else { Write-Warn2 ("ssh 反向隧道进程：0 个（-R {0}）" -f $port) }

    $health = Test-Health
    if ($health) { Write-Ok ("本机 Worker /health：{0}" -f "$health".Trim()) }
    else { Write-Warn2 ("本机 Worker /health 无响应（已试 {0}:{1}；Worker 没在跑？）" -f ((Get-WorkerHosts) -join "/"), (Get-WorkerPort)) }

    # Linux 侧的队列：这台 PC 的隧道口接到的活就来自这里。慢（一次 ssh，1 s 上下），
    # 所以放在最后；取不到只报一行，不影响上面本机状态的可读性。
    # 默认只显示**本机隧道口**的线（-Channel $port）：每台 PC 看自己的隧道口，各看各的。
    Write-Host ""
    Write-Head ("Linux 侧下载队列（本机隧道口 {0} 的在下载任务）" -f $port)
    $view = Invoke-LinuxView -Limit 5 -Channel $port -WithPool
    Write-Participation -Pool $view.Pool -Port $port
    Show-TasksBlock -Frame $view.Frame -MaxActive 8

    if (Test-Path $TunnelLog) {
        Write-Host ""
        Write-Host "  隧道守护最近 5 行：" -ForegroundColor DarkGray
        Get-Content -Path $TunnelLog -Tail 5 | ForEach-Object { Write-Host ("    " + $_) -ForegroundColor DarkGray }
    }
}

function Get-ProxyKind {
    # Human label for a proxy url, for the status/confirm lines.
    param([string]$Url)
    if ("$Url" -match '^socks5h://') { return "SOCKS5，代理解析 DNS" }
    if ("$Url" -match '^socks5://')  { return "SOCKS5，本机解析 DNS" }
    return "HTTP"
}

function Test-LoopbackProxy {
    # 私人代理住在 127.0.0.1:7897。任何指向本机回环的代理一律拒绝 —— 下载流量
    # 不得走私人代理（项目硬规则），在命令行这一层就先挡住；worker.py 那边还会
    # 再挡一次（删掉配置里的该项），两层都拦。
    param([string]$HostName)
    $h = "$HostName".Trim().Trim('[', ']').ToLower()
    if ($h -eq "localhost" -or $h -eq "::1") { return $true }
    if ($h -match '^127\.') { return $true }
    return $false
}

function Resolve-ProxySpec {
    # "http://ip:port" / "socks5h://ip:port" / "ip:port" -> 归一化 url；非法返回 $null（原因已打印）
    param([string]$Text)

    $t = "$Text".Trim()
    if (-not $t) { return $null }
    if ($t -notmatch '://') { $t = "http://" + $t }

    if ($t -notmatch '^(?<scheme>http|socks5|socks5h)://(?:(?<user>[^@/]*)@)?(?<host>[^:/?#]+)(?::(?<port>\d{1,5}))$') {
        Write-Warn2 ("代理地址无效：{0}" -f $Text)
        Write-Info  "  正确形式：http://ip:port 或 socks5h://ip:port（必须带端口）"
        return $null
    }

    $scheme = $Matches['scheme'].ToLower()
    $phost  = $Matches['host']
    $pport  = $Matches['port']
    $puser  = $Matches['user']

    if (Test-LoopbackProxy $phost) {
        Write-Warn2 ("拒绝：{0} 指向本机回环 —— 那是本机私人代理，下载流量不得使用" -f $phost)
        return $null
    }

    $portNum = 0
    if (-not [int]::TryParse($pport, [ref]$portNum) -or $portNum -lt 1 -or $portNum -gt 65535) {
        Write-Warn2 ("代理端口无效：{0}" -f $pport)
        return $null
    }

    $cred = ""
    if ($puser) { $cred = $puser + "@" }

    return ("{0}://{1}{2}:{3}" -f $scheme, $cred, $phost, $portNum)
}

function Set-ProxyInConfig {
    # 只改 proxy.url，并把私人代理钉死为 disabled；其余键原样保留。
    # UTF-8 无 BOM 写回（与安装器一致；worker_config.py 用 utf-8-sig 读，两种都吃）。
    param([string]$Url)

    $cfg = Get-Cfg
    if ($null -eq $cfg) { Write-Warn2 "读不到 worker-config.json（先跑安装器）"; return $false }

    $proxy = $cfg.proxy
    if ($null -eq $proxy) {
        $proxy = New-Object psobject
        $cfg | Add-Member -NotePropertyName proxy -NotePropertyValue $proxy -Force
    }

    $proxy | Add-Member -NotePropertyName url -NotePropertyValue "$Url" -Force

    $private = $proxy.windows_private
    if ($null -eq $private) {
        $private = New-Object psobject
        $proxy | Add-Member -NotePropertyName windows_private -NotePropertyValue $private -Force
    }
    $private | Add-Member -NotePropertyName enabled -NotePropertyValue $false -Force

    try {
        $json = $cfg | ConvertTo-Json -Depth 5
        [System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))
        return $true
    }
    catch {
        Write-Warn2 ("写 worker-config.json 失败：{0}" -f $_)
        return $false
    }
}

function Show-ProxySetting {
    $cfg = Get-Cfg
    $url = ""
    if ($null -ne $cfg -and $null -ne $cfg.proxy) { $url = "$($cfg.proxy.url)".Trim() }

    if (-not $url) {
        Write-Info "当前下载出口：直连 DIRECT（未配置代理）"
    }
    else {
        Write-Ok ("当前下载出口：WORKER_PROXY {0}（{1}）—— 优先走它，失败才回退直连 DIRECT" -f $url, (Get-ProxyKind $url))
    }

    $privateOn = $false
    if ($null -ne $cfg -and $null -ne $cfg.proxy -and $null -ne $cfg.proxy.windows_private) {
        $privateOn = [bool]$cfg.proxy.windows_private.enabled
    }
    if ($privateOn) {
        Write-Warn2 "私人代理：配置里是启用状态 —— 下载大文件永不使用；建议改回禁用"
    }
    else {
        Write-Info "私人代理：禁用（下载不占用）"
    }
}

function Invoke-Proxy {
    param([string]$Url)

    Write-Head "download-worker proxy - 本机下载出口（代理 / 直连）"

    if (-not $Url -or -not "$Url".Trim()) {
        Show-ProxySetting
        Write-Host ""
        Write-Info "设置：download-worker proxy http://ip:port"
        Write-Info "      download-worker proxy socks5h://ip:port"
        Write-Info "      download-worker proxy off        （清除，只走直连）"
        return
    }

    $value = "$Url".Trim()
    if (@("off", "none", "disable", "clear", "-") -contains $value.ToLower()) {
        $value = ""
    }
    else {
        $normalized = Resolve-ProxySpec $value
        if (-not $normalized) { return }
        $value = $normalized
    }

    if (-not (Set-ProxyInConfig -Url $value)) { return }

    if ($value) {
        Write-Ok ("已设置下载代理：{0}（{1}）" -f $value, (Get-ProxyKind $value))
        Write-Info "下个请求即生效（worker.py 每次请求都重读配置，不需要 restart）"
    }
    else {
        Write-Ok "已清除下载代理：只走直连 DIRECT"
    }

    Write-Host ""
    Show-ProxySetting
    Write-Host ""
    Write-Info "提示：改完可以跑一次 download-worker status，确认「下载出口」显示的是你要的那条"
    Write-Info "      想知道这条路到底多快：download-worker test 256M"
}

function ConvertTo-TestBytes {
    # "download-worker test" 的样本大小：纯数字=字节（与 curl 的 -r 0-<n> 同款写法），
    # 也可带单位 256M / 1G / 512K。解析不出来返回 0。
    param([string]$Text)

    $t = "$Text".Trim().ToUpperInvariant()
    if (-not $t) { return 0 }

    $m = [regex]::Match($t, '^(?<num>\d+(?:\.\d+)?)\s*(?<unit>[KMGT]?)(?:I?B?)?$')
    if (-not $m.Success) { return 0 }

    $num = [double]$m.Groups["num"].Value
    $multiplier = [int64]1

    switch ($m.Groups["unit"].Value) {
        "K" { $multiplier = [int64]1KB }
        "M" { $multiplier = [int64]1MB }
        "G" { $multiplier = [int64]1GB }
        "T" { $multiplier = [int64]1TB }
    }

    return [int64]($num * $multiplier)
}

function Invoke-Test {
    # 实测这条 PC 的下载速度 —— 与手工验证时用的命令等价：
    #   curl.exe --proxy <代理> -L -r 0-<n-1> -o NUL -w "..." <url>
    # 默认走配置里的代理（worker.py 实际用的那条路）；-Direct 绕过代理做对比。
    # 数据写进 NUL，不落盘；进度表由 curl 直接打给终端，汇总行由本函数解析后打印。
    param(
        [string]$Size,
        [string]$Url,
        [switch]$Direct
    )

    Write-Head "download-worker test - 实测这台 PC 的下载速度"

    $bytes = ConvertTo-TestBytes $Size

    if ($bytes -le 0) {
        Write-Warn2 ("样本大小无法解析：{0}" -f $Size)
        Write-Info  "  写法：268435455（字节，同 curl 的 -r 0-268435455）或 256M / 1G"
        Write-Info  "  例子：download-worker test 268435455 `"$DefaultTestUrl`""
        return
    }

    if ($bytes -gt 10GB) {
        Write-Warn2 ("单次样本上限 10 GiB（你给的是 {0:N0} 字节）—— 测速没必要拉这么多" -f $bytes)
        return
    }

    if (-not "$Url".Trim()) { $Url = $DefaultTestUrl }
    $Url = "$Url".Trim()

    if ($Url -notmatch '^https?://') {
        Write-Warn2 ("URL 必须是 http/https：{0}" -f $Url)
        return
    }

    if (-not (Test-Path $CurlExe)) {
        Write-Warn2 ("找不到 {0}（Windows 10 1803+ 自带 curl.exe）" -f $CurlExe)
        Write-Info  "  没有的话装一个 curl 再用；worker 本身不依赖它"
        return
    }

    $proxy = ""
    if (-not $Direct) {
        $cfg = Get-Cfg
        if ($null -ne $cfg -and $null -ne $cfg.proxy) { $proxy = "$($cfg.proxy.url)".Trim() }

        # 配置若是被人手工塞回环（本机私人代理），worker.py 会拒绝它并退回直连 ——
        # 这里照做，免得测速把私人代理和自己的流量用起来。
        if ($proxy -and $proxy -match '^[a-z0-9]+://(?:[^@/]*@)?(?<host>[^:/?#]+)') {
            if (Test-LoopbackProxy $Matches['host']) {
                Write-Warn2 ("配置里的代理 {0} 指向本机回环 —— 私人代理不可用，本次按直连测" -f $proxy)
                $proxy = ""
            }
        }
    }

    $exitLabel = "DIRECT（直连）"
    if ($proxy) { $exitLabel = ("WORKER_PROXY {0}" -f $proxy) }

    Write-Info ("目标：{0}" -f $Url)
    Write-Info ("样本：{0:N0} 字节（{1:N1} MiB）" -f $bytes, ($bytes / 1MB))
    Write-Info ("出口：{0}" -f $exitLabel)

    if ($bytes -lt 1MB) {
        Write-Warn2 "样本不到 1 MiB，速度读数意义不大（至少 256M 才有代表性）"
    }

    if ($proxy -and -not $Direct) {
        Write-Info "想对比直连：加 -Direct"
    }
    elseif (-not $proxy -and -not $Direct) {
        Write-Info "这次测的是直连（没配代理，或配置里的代理被拒）；配代理用 download-worker proxy <url>"
    }

    Write-Host ""

    # %{...} 变量由 curl 填充；这一行走 stdout，进度表走 stderr（照旧打在终端上）。
    $format = "DW|%{http_code}|%{speed_download}|%{time_total}|%{size_download}|%{url_effective}"
    $arguments = @(
        "--location",
        "--output", "NUL",
        "--range", ("0-{0}" -f ($bytes - 1)),
        "--write-out", $format,
        "--connect-timeout", "30",
        "--max-time", "1800"
    )

    if ($proxy) { $arguments += @("--proxy", $proxy) }
    $arguments += $Url

    $outFile = Join-Path $env:TEMP ("dw-test-{0}.txt" -f $PID)
    $exitCode = -1
    $line = ""

    # curl 的进度表写在 stderr 上：这里只把 stdout（汇总行）收进文件，stderr 不重定向，
    # 于是进度照旧实时显示。EAP 临时降为 Continue —— 原生程序写 stderr 不该中断脚本。
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        & $CurlExe @arguments 1>$outFile
        $exitCode = $LASTEXITCODE

        if (Test-Path $outFile) {
            $line = @(Get-Content -Path $outFile -ErrorAction SilentlyContinue) |
                    Where-Object { "$_" -like "DW|*" } | Select-Object -First 1
        }
    }
    finally {
        $ErrorActionPreference = $previous
        Remove-Item -Path $outFile -Force -ErrorAction SilentlyContinue
    }

    Write-Host ""

    if (-not $line) {
        Write-Warn2 ("curl 没给出汇总行（exit code {0}）—— 上面的 stderr 里有它的报错" -f $exitCode)
        return
    }

    $parts = $line -split '\|'
    if ($parts.Count -lt 6) {
        Write-Warn2 ("汇总行看不懂：{0}" -f $line)
        return
    }

    $httpCode  = $parts[1]
    $speedBps  = 0.0
    $timeTotal = 0.0
    $received  = 0L

    # curl 一律输出点号小数；用 invariant culture 解析，免得逗号小数区（如 de-DE）把 1234.56 读成 123456
    [void][double]::TryParse($parts[2], [System.Globalization.NumberStyles]::Float, `
        [System.Globalization.CultureInfo]::InvariantCulture, [ref]$speedBps)
    [void][double]::TryParse($parts[3], [System.Globalization.NumberStyles]::Float, `
        [System.Globalization.CultureInfo]::InvariantCulture, [ref]$timeTotal)
    [void][int64]::TryParse($parts[4], [System.Globalization.NumberStyles]::Integer, `
        [System.Globalization.CultureInfo]::InvariantCulture, [ref]$received)

    $mibPerSecond = $speedBps / 1MB
    $mbPerSecond  = $speedBps / 1000000

    if ($httpCode -ne "206" -and $httpCode -ne "200") {
        Write-Warn2 ("HTTP {0} —— 没拿到数据（206 才算正常；Range 不被支持时可能是 200）" -f $httpCode)
    }

    if ($speedBps -gt 0) {
        Write-Ok ("实测速度：{0:N2} MiB/s（{1:N2} MB/s）" -f $mibPerSecond, $mbPerSecond)
    }
    else {
        Write-Warn2 "实测速度为 0 —— 数据没下来（看上面的 curl 报错）"
    }

    Write-Info ("耗时：{0:N1} s   接收：{1:N0} 字节（{2:N1} MiB）   HTTP {3}" -f `
        $timeTotal, $received, ($received / 1MB), $httpCode)

    if ($speedBps -gt 0) {
        $secondsPerGiB = 1GB / $speedBps
        Write-Info ("按这个速度：1 GiB 约需 {0}，10 GiB 约需 {1}" -f `
            (Format-Duration $secondsPerGiB), (Format-Duration ($secondsPerGiB * 10)))
    }

    if ($speedBps -gt 0 -and $speedBps -lt 256KB) {
        Write-Warn2 "比预期慢很多 —— 确认走的是你要的那条出口（download-worker proxy 看配置，-Direct 做对比）"
    }
}

function Invoke-Process {
    # 实时看 Linux 侧在下什么：最近 5 条已完成 + 当前下载的进度与速度，原地刷新。
    # Ctrl+C 直接退出 —— ssh 与 PowerShell 同属一个控制台，控制台中断会同时送到两边，
    # 本地 ssh 一死，远端 python 下一次写 stdout 就拿到 EPIPE 自己收摊（不会留孤儿）。
    param([int]$Interval = 2, [int]$Recent = 5)

    $cfg = Get-Cfg
    if ($null -eq $cfg -or -not $cfg.linux_user -or -not $cfg.linux_host) {
        Write-Warn2 "linux_user/linux_host 未配置，读不到 Linux 侧任务"
        return
    }

    # 只看**本机隧道口**的线（每台 PC 各自的隧道口），和 status 同一口径。
    $port = Get-Port
    Write-Head ("download-worker process - Linux 侧下载任务（实时，仅本机隧道口 {0}）" -f $port)

    if ($Interval -lt 1) { $Interval = 1 }
    if ($Recent -lt 1) { $Recent = 1 }

    $taskScript = Get-TasksPath
    $target  = "{0}@{1}" -f $cfg.linux_user, $cfg.linux_host
    $command = "python3 {0} --watch {1} --limit {2} --channel {3}" -f $taskScript, $Interval, $Recent, $port

    Write-Info ("来源：{0}    Linux 脚本：{1}" -f $target, $taskScript)
    Write-Info ("刷新间隔：{0} s    最近完成：{1} 条" -f $Interval, $Recent)
    Write-Info "Ctrl+C 退出。"
    Write-Host ""

    # 表格顶端：每帧回到这一行原地重绘，而不是一路往下滚（同 curl 进度表的做法）。
    $top = 0
    try { $top = [Console]::CursorTop } catch { }
    $drawn = 0
    $frames = 0

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $SshExe -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 $target $command 2>&1 |
            ForEach-Object {
                $line = "$_"
                if ($line.TrimStart() -notlike "{*") {
                    # ssh 自己的报错行 / 远端 traceback：直接打出来，别吞
                    Write-Host ("    " + $line) -ForegroundColor DarkGray
                    return
                }

                $frame = $null
                try { $frame = $line | ConvertFrom-Json }
                catch { Write-Host ("    帧解析失败：" + $_) -ForegroundColor Yellow; return }

                $frames++
                $lines = @(Get-TasksLines -Frame $frame -Recent $Recent -MaxActive 6)

                $width = 100
                try { if ([Console]::WindowWidth -gt 40) { $width = [Console]::WindowWidth - 1 } } catch { }

                # 一帧比窗口还高就会把顶端顶出屏幕、原地重绘随之错位 —— 先按窗口高度截断。
                $maxLines = 24
                try { $maxLines = [Math]::Max(8, [Console]::WindowHeight - $top - 1) } catch { }

                $texts = @(("  [ {0}   第 {1} 帧 ]" -f $frame.generated, $frames))
                foreach ($l in $lines) { $texts += "$($l.Text)" }
                if ($texts.Count -gt $maxLines) {
                    $keep = $maxLines - 1
                    $texts = @($texts[0..($keep - 1)]) + `
                             @("          … 还有 {0} 行没显示（窗口高度不够，Ctrl+C 退出）" -f ($texts.Count - $keep))
                }

                try { [Console]::SetCursorPosition(0, $top) }
                catch {
                    # 输出被重定向时没有光标可定位：退化成顺序打印
                    foreach ($t in $texts) { Write-Host $t }
                    return
                }

                $blank = " " * $width
                foreach ($text in $texts) {
                    $shown = $text
                    if ($shown.Length -gt $width) { $shown = $shown.Substring(0, $width) }
                    [Console]::Write($shown.PadRight($width))
                    [Console]::Write([Environment]::NewLine)
                }
                # 上一帧比这一帧长时，把多出来的行擦掉
                for ($i = $texts.Count; $i -lt $drawn; $i++) {
                    [Console]::Write($blank)
                    [Console]::Write([Environment]::NewLine)
                }
                $drawn = $texts.Count
            }
    }
    finally {
        $ErrorActionPreference = $previous
    }

    if ($frames -eq 0) {
        Write-Warn2 "一帧都没拿到 —— Linux 侧不可达，或 dw_tasks.py 不在默认路径"
        Write-Info  ("  手动确认：ssh {0} `"python3 {1} --text`"" -f $target, $taskScript)
    }
    else {
        Write-Host ""
        Write-Ok ("已退出（共 {0} 帧）" -f $frames)
    }
}

function Format-Duration {
    # 秒数 -> "12.3 分钟" / "1.4 小时" / "2.1 天"
    param([double]$Seconds)

    if ($Seconds -lt 60) { return ("{0:N0} 秒" -f $Seconds) }
    if ($Seconds -lt 3600) { return ("{0:N1} 分钟" -f ($Seconds / 60)) }
    if ($Seconds -lt 86400) { return ("{0:N1} 小时" -f ($Seconds / 3600)) }
    return ("{0:N1} 天" -f ($Seconds / 86400))
}

function Stop-InstallProcesses {
    $targets = @()
    $targets += Get-InstallProcess -Pattern "_supervisor.py"
    $targets += Get-InstallProcess -Pattern "start_worker.py"
    $targets += Get-InstallProcess -Pattern "worker.py"
    $targets += @(Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue |
                  Where-Object { $_.CommandLine -like ("*-R " + (Get-Port) + ":*") })

    $stopped = 0
    foreach ($p in ($targets | Sort-Object ProcessId -Unique)) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            $stopped++
        }
        catch {
            Write-Warn2 ("结束进程 {0} 失败：{1}" -f $p.ProcessId, $_)
        }
    }
    if ($stopped -gt 0) { Write-Ok ("已结束本安装的进程：{0} 个" -f $stopped) }
    else { Write-Info "没有需要结束的进程" }
}

function Set-TasksPaused {
    param([bool]$Paused)
    $failed = $false
    foreach ($name in @($WorkerTask, $TunnelTask)) {
        if ($null -eq (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) { continue }
        try {
            if ($Paused) { Stop-ScheduledTask -TaskName $name -ErrorAction Stop; Disable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null }
            else         { Enable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null; Start-ScheduledTask -TaskName $name -ErrorAction Stop }
        }
        catch { $failed = $true; Write-Warn2 ("计划任务 {0} 操作失败（需要管理员权限？）：{1}" -f $name, $_.Exception.Message) }
    }
    if ($failed) {
        Write-Info "提示：以管理员身份重跑本命令可一并更新计划任务的启用状态；"
        Write-Info "      暂停/恢复的实际效果由 paused.flag 保证，不受此影响。"
    }
}

function Invoke-Off {
    Write-Head "download-worker off - 本机退出下载（worker + 隧道）"
    New-Item -ItemType File -Path $PauseFlag -Force | Out-Null
    Write-Ok "已写入 paused.flag（两个 supervisor 都不会再拉起任何东西）"
    Stop-InstallProcesses
    Set-TasksPaused -Paused $true
    Sync-LinuxRegistry -Enabled $false
    Write-Host ""
    Write-Ok "本机已下线。恢复用：download-worker on"
}

function Invoke-On {
    Write-Head "download-worker on - 本机重新加入下载"
    if (Test-Path $PauseFlag) {
        Remove-Item -Path $PauseFlag -Force
        Write-Ok "已删除 paused.flag"
    }

    # Enable + start both tasks.  If a task was left running while paused, its supervisor
    # picks the flag removal up within 30 s on its own; starting an already running task
    # is harmless (MultipleInstances = IgnoreNew).
    Set-TasksPaused -Paused $false

    # Print every round: this loop used to be silent for its whole run and users read
    # that as "the command hung".
    Write-Info ("等待 Worker 起来（探 {0}:{1}；最多 15 轮）…" -f ((Get-WorkerHosts) -join "/"), (Get-WorkerPort))
    $ok = $false
    for ($i = 1; $i -le 15; $i++) {
        Start-Sleep -Seconds 2
        $health = Test-Health -TimeoutSeconds 3
        if ($health -and "$health" -match '"status"\s*:\s*"ok"') {
            Write-Info ("  第 {0,2}/15 轮：OK" -f $i)
            $ok = $true
            break
        }
        Write-Info ("  第 {0,2}/15 轮：无响应" -f $i)
    }
    if ($ok) { Write-Ok "Worker /health 正常" }
    else { Write-Warn2 "15 轮探测内没等到 /health —— 看 logs\worker.log 与 logs\worker_supervisor.log（后者写守护为什么不拉起 Worker）" }

    Sync-LinuxRegistry -Enabled $true
    Write-Host ""
    Write-Ok "本机已上线。查看状态：download-worker status"
}

function Invoke-Restart {
    Write-Head "download-worker restart - 重启 worker + 隧道（不改变 on/off 状态）"
    if (Test-Path $PauseFlag) { Write-Warn2 "当前处于暂停（paused.flag）；restart 不会解除暂停"; return }
    Stop-InstallProcesses
    Set-TasksPaused -Paused $false
    Start-Sleep -Seconds 5
    Write-Ok "已重启（状态：download-worker status）"
}

switch ($Action) {
    "status"  { Show-Status }
    "off"     { Invoke-Off }
    "on"      { Invoke-On }
    "restart" { Invoke-Restart }
    "proxy"   { Invoke-Proxy -Url $Arg1 }
    "test"    { Invoke-Test -Size $Arg1 -Url $Arg2 -Direct:$Direct }
    "process" { Invoke-Process -Interval $ProcessInterval }
}
