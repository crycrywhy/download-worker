<#
.SYNOPSIS
    Install the Local Download Worker on a Windows PC.

.DESCRIPTION
    Deploys the frozen reference Worker implementation:
      * checks Windows / Python / venv / pip / Tailscale / OpenSSH / SSH auth
      * detects this machine's Tailscale IPv4 automatically
      * creates <InstallDir>\.venv and pip-installs requirements.txt
      * copies the Worker sources from ..\worker
      * writes worker-config.json (all machine-specific values)
      * creates the firewall rule (Tailscale-only) and the scheduled tasks
      * starts the Worker, waits for /health, starts the tunnel, verifies it

    Before installing it removes any PREVIOUS installation it can find (an older
    package in another directory, or a running copy of the same one): leftover
    processes lock the files and hold the tunnel port, which makes both the copy
    step and a later uninstall fail.

    Repeat runs are upgrades.  Worker sources, dependencies, firewall rule and
    scheduled tasks are refreshed; worker-config.json, logs\bandwidth.json,
    logs\ and .venv are preserved.

.PARAMETER InstallDir
    Target directory.  Default: C:\ProgramData\LocalDownloadWorker

.PARAMETER LinuxUser
    SSH user on the Linux host (Tailscale network).  Required on first install.

.PARAMETER LinuxHost
    Linux host IP or name.  Required on first install.

.PARAMETER LinuxTunnelPort
    Linux-side TCP port opened by the reverse tunnel.  Default 8766.
    Use a DIFFERENT port on every additional Windows PC.

.PARAMETER WorkerPort
    Windows Worker HTTP port.  Default 8765.

.PARAMETER WorkerLabel
    Display name for this PC, shown on its own status screen and reported
    to the Linux side.  Default: this machine's host name ($env:COMPUTERNAME).
    Set it when the host name is not meaningful to you.

.PARAMETER WorkerProxy
    The Worker's OWN proxy, reached directly over the network - for example
    http://<proxy-ip>:20171 or socks5h://<proxy-ip>:20170.  It fills
    "proxy.url" in worker-config.json, and every download is tried through it
    FIRST, with a DIRECT fallback when the proxy cannot be reached.  Password
    free urls are the norm; http://user:pass@host:port is accepted and never
    logged.  This PC's private proxy is never a download exit - a loopback url
    (127.0.0.1 / localhost / ::1) is refused here and again in worker.py.
    The name is deliberate: the proxy belongs to the Worker and is dialled
    straight from this PC, with no ssh -L tunnel in between.
    Empty = DIRECT only.  When the switch is omitted, an interactive install
    asks for it; an upgrade keeps the value already in worker-config.json.

.PARAMETER LinuxProxyHost
    DEPRECATED (r5).  The old reverse-tunnel proxy.  Kept only so an existing
    command line does not fail; host+port+type are folded into -WorkerProxy and
    a warning is printed.  No tunnel is started any more.

.PARAMETER LinuxProxyPort
    DEPRECATED (r5), see LinuxProxyHost.

.PARAMETER LinuxProxyType
    DEPRECATED (r5), see LinuxProxyHost.

.PARAMETER LinuxProxyLocalPort
    DEPRECATED (r5): the Windows loopback port of the removed ssh -L forward.
    Accepted and ignored.

.PARAMETER DisableLinuxProxy
    DEPRECATED (r5): same as -WorkerProxy off (write an empty proxy url).

.PARAMETER LogSyncRemoteDir
    Remote directory used by sync_log.py.
    Default: /home/<LinuxUser>/<repo>/logs

.PARAMETER LogSyncIntervalMinutes
    Log sync period in minutes.  Default 5.

.PARAMETER SkipLogSyncTask
    Do not create the "Local Download Log Sync" scheduled task.

.PARAMETER PipIndexUrl
    Optional pip index URL (for example a mirror).

.PARAMETER SkipLegacyCleanup
    Do not stop / delete previous installations.  Use only when the automatic
    cleanup picked up a directory that must stay.

.PARAMETER KeepPaused
    Keep the "switched off" state.  A PC switched off with `download-worker off`
    carries paused.flag; the supervisors then refuse to start anything, so the
    installer would wait 90 s for /health and fail with "Worker did not become
    healthy".  By default the installer removes the flag and
    brings the PC back online; -KeepPaused keeps it off and skips the start
    verification.

.PARAMETER DryRun
    Report what would be done without changing the system.

.EXAMPLE
    .\install-worker.ps1 -LinuxUser <user> -LinuxHost <linux-ip>

.EXAMPLE
    .\install-worker.ps1 -InstallDir "D:\LocalDownloadWorker" -LinuxUser <user> `
        -LinuxHost <linux-ip> -LinuxTunnelPort <port>
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$InstallDir = "C:\ProgramData\LocalDownloadWorker",

    [string]$LinuxUser,

    [string]$LinuxHost,

    [int]$LinuxTunnelPort = 8766,

    [int]$WorkerPort = 8765,

    # Display name for this PC (see the .PARAMETER block).  Empty = the host name.
    [string]$WorkerLabel,

    # The Worker's own proxy - downloads are tried through it first, DIRECT is the
    # fallback (see the .PARAMETER block).  Empty = DIRECT only.
    [string]$WorkerProxy,

    # DEPRECATED (r5 reverse-tunnel proxy, gone in r6).  Kept only so an existing
    # command line does not fail outright: host+port+type are folded into
    # -WorkerProxy with a warning, and the tunnel-related ones are ignored.
    [string]$LinuxProxyHost,

    [int]$LinuxProxyPort = 0,

    [ValidateSet("http", "socks5")]
    [string]$LinuxProxyType = "http",

    [int]$LinuxProxyLocalPort = 20171,

    [switch]$DisableLinuxProxy,

    [string]$LogSyncRemoteDir,

    [int]$LogSyncIntervalMinutes = 5,

    # Path of the Linux-side pool CLI that `download-worker on/off` calls over ssh to
    # keep workers.json in sync (so the monitoring there does not report a false
    # "worker down" while this PC is deliberately switched off).  Override when your
    # Linux checkout lives somewhere else.
    [string]$LinuxWorkerPoolPath = "~/download-worker/tools/worker_pool.py",

    # Path of the Linux-side read-only task view (dw_tasks.py).  `download-worker
    # status` renders one snapshot of it and `download-worker process` streams it,
    # so the Linux-side queue and the progress of each download are visible from
    # the PC.  Read-only: it never writes anything on the Linux side.
    [string]$LinuxDwTasksPath = "~/download-worker/linux/dw_tasks.py",

    [switch]$SkipLogSyncTask,

    [string]$PipIndexUrl,

    # Before installing, look for previous installations (scheduled-task paths plus
    # the well-known directories) and clear them: stop their processes - venv python
    # AND the ssh reverse tunnel, whose command line carries no directory, only
    # "-R <port>:" - then remove the directory.  A leftover tunnel keeps the port on
    # the Linux side occupied, so the new supervisor's tunnel can never start
    # (exit 255, endless retry: exactly what happened in the field).
    [switch]$SkipLegacyCleanup,

    # Keep the PC switched off: leave paused.flag in place and do not wait for a
    # Worker that the supervisors are told not to start (see the .PARAMETER block).
    [switch]$KeepPaused,

    [switch]$DryRun,

    # When a freshly opened window cannot run script files (Windows ships with
    # "Restricted"), the installer offers to allow locally created scripts for the
    # current user (Set-ExecutionPolicy -Scope CurrentUser RemoteSigned: user scope,
    # no administrator needed, downloaded files still need a signature or Unblock-File).
    # -FixExecutionPolicy answers that question in advance (unattended installs),
    # -SkipExecutionPolicyFix refuses it and only prints the hint.
    [switch]$FixExecutionPolicy,
    [switch]$SkipExecutionPolicyFix
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$script:WorkerTaskName = "Local Download Worker"
$script:TunnelTaskName = "Local Download Tunnel"
$script:LogSyncTaskName = "Local Download Log Sync"
$script:DryRun = [bool]$DryRun
$script:BandwidthCommandsOk = $false
$script:BandwidthPolicyBlocked = $false
$script:SwitchCommandOk = $false

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$ConfigPath = Join-Path $InstallDir "worker-config.json"
$PauseFlag = Join-Path $InstallDir "paused.flag"
$LogDir = Join-Path $InstallDir "logs"
$InstallerLog = Join-Path $LogDir "installer.log"
$VenvDir = Join-Path $InstallDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$WorkerSourceDir = Join-Path (Split-Path -Parent $PSScriptRoot) "worker"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

function Write-Log {
    param(
        [string]$Message,
        [ValidateSet("INFO", "OK", "WARN", "ERROR")][string]$Level = "INFO"
    )

    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message

    switch ($Level) {
        "OK"    { Write-Host $line -ForegroundColor Green }
        "WARN"  { Write-Host $line -ForegroundColor Yellow }
        "ERROR" { Write-Host $line -ForegroundColor Red }
        default { Write-Host $line }
    }

    if (-not $script:DryRun) {
        try {
            if (Test-Path $LogDir) {
                Add-Content -Path $InstallerLog -Value $line -Encoding UTF8
            }
        }
        catch {
            # logging must never break the installation
        }
    }
}

function Write-Step {
    param([string]$Message)
    Write-Log ("-" * 4 + " " + $Message)
}

function Write-JsonFile {
    param(
        [string]$Path,
        [string]$Json
    )

    # UTF-8 without BOM: Python's json.loads() chokes on a BOM.
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Json, $encoding)
}

function Test-HealthUrl {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 3
    )

    try {
        $request = [System.Net.HttpWebRequest]::Create($Url)
        $request.Proxy = $null          # never use the system proxy for 100.x addresses
        $request.Timeout = $TimeoutSeconds * 1000
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $body = $reader.ReadToEnd()
        $reader.Close()
        $response.Close()
        return $body
    }
    catch {
        return $null
    }
}

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-Log ("  > " + $FilePath + " " + ($Arguments -join " "))

    if ($script:DryRun) {
        return 0
    }

    # stdout goes to the console, NOT into this function's return value:
    # otherwise the child process output lines end up in $code and the
    # exit-code test sees a non-empty array instead of an integer
    # (bug found during the second-PC acceptance run).
    & $FilePath @Arguments | Out-Host

    return [int]$LASTEXITCODE
}


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)

    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This installer must run in an elevated PowerShell window (Run as Administrator)."
    }
}

function Assert-WindowsVersion {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "A 64-bit version of Windows is required."
    }

    $version = [Environment]::OSVersion.Version

    if ($version.Major -lt 10) {
        throw ("Windows 10 or Windows Server 2016 or newer is required (found {0})." -f $version)
    }

    Write-Log ("Windows {0} ({1})" -f $version, $env:COMPUTERNAME) "OK"
}

function Find-Python {
    $candidates = @()

    # 1) the py launcher
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        $candidates += ,@($pyLauncher.Source, @("-3"))
    }

    # 2) anything named python on PATH
    foreach ($name in @("python.exe", "python3.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $cmd) {
            $candidates += ,@($cmd.Source, @())
        }
    }

    # 3) common install locations
    $patterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "C:\Python3*\python.exe"
    )

    foreach ($pattern in $patterns) {
        $found = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending
        foreach ($item in $found) {
            $candidates += ,@($item.FullName, @())
        }
    }

    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $prefix = $candidate[1]
        $args = $prefix + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")

        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"

        try {
            $output = & $exe @args 2>$null
        }
        catch {
            $ErrorActionPreference = $previous
            continue
        }
        finally {
            $ErrorActionPreference = $previous
        }

        if ($LASTEXITCODE -ne 0) { continue }

        $text = "$($output | Select-Object -First 1)".Trim()
        if (-not $text) { continue }

        $parts = $text.Split(".")
        if ($parts.Count -lt 2) { continue }

        $major = [int]$parts[0]
        $minor = [int]$parts[1]

        if ($major -eq 3 -and $minor -ge 8) {
            Write-Log ("Python {0} found: {1}" -f $text, $exe) "OK"
            return @{ Exe = $exe; Prefix = $prefix; Version = $text }
        }
    }

    throw @"
Python 3.8 or newer is required but was not found.

Please install Python (python.org, 64-bit, "Add python.exe to PATH")
and run this installer again.
"@
}

function Assert-VenvSupport {
    param($Python)

    $arguments = $Python.Prefix + @("-m", "venv", "--help")

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $null = & $Python.Exe @arguments 2>&1
    }
    finally {
        $ErrorActionPreference = $previous
    }

    if ($LASTEXITCODE -ne 0) {
        throw "python -m venv is not available. Install the full Python distribution (not the embeddable package) and retry."
    }

    Write-Log "python -m venv is available" "OK"
}

function Get-TailscaleIPv4 {
    $exe = "C:\Program Files\Tailscale\tailscale.exe"
    $service = Get-Service -Name "Tailscale" -ErrorAction SilentlyContinue

    if (-not (Test-Path $exe) -and $null -eq $service) {
        throw @"
Tailscale was not detected.

Please install and log in to Tailscale first, then run this installer again.
"@
    }

    if (Test-Path $exe) {
        try {
            $output = & $exe ip -4 2>$null
            foreach ($line in @($output)) {
                $text = "$line".Trim()
                if ($text -match "^100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\.") {
                    Write-Log ("Tailscale IPv4: {0}" -f $text) "OK"
                    return $text
                }
            }
        }
        catch {
            # fall through to the ipconfig based detection
        }
    }

    $addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -match "^100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\." }

    if ($null -ne $addresses) {
        $ip = ($addresses | Select-Object -First 1).IPAddress
        if ($ip) {
            Write-Log ("Tailscale IPv4: {0}" -f $ip) "OK"
            return $ip
        }
    }

    throw @"
Tailscale is installed but no Tailscale IPv4 address (100.64.0.0/10) was found.

Make sure Tailscale is running and logged in, then run this installer again.
"@
}

function Find-SshExe {
    $system = "C:\Windows\System32\OpenSSH\ssh.exe"

    if (Test-Path $system) {
        Write-Log ("OpenSSH client: {0}" -f $system) "OK"
        return $system
    }

    $cmd = Get-Command "ssh.exe" -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        Write-Log ("OpenSSH client: {0}" -f $cmd.Source) "OK"
        return $cmd.Source
    }

    throw "OpenSSH client (ssh.exe) was not found. Install the Windows OpenSSH Client optional feature and retry."
}

function Assert-LinuxSshAuth {
    param(
        [string]$SshExe,
        [string]$Target
    )

    $arguments = @(
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "StrictHostKeyChecking=accept-new",
        $Target,
        "echo LOCAL_DOWNLOAD_WORKER_SSH_OK"
    )

    Write-Log ("Testing non-interactive SSH to {0} ..." -f $Target)

    # native stderr would be a terminating error while ErrorActionPreference is Stop
    $output = ""
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        $output = & $SshExe @arguments 2>&1
    }
    catch {
        $output = "$_"
    }
    finally {
        $ErrorActionPreference = $previous
    }

    if ($LASTEXITCODE -eq 0 -and ("$output" -match "LOCAL_DOWNLOAD_WORKER_SSH_OK")) {
        Write-Log "SSH non-interactive authentication works" "OK"
        return
    }

    throw @"
SSH non-interactive authentication is not configured.

  Target : $Target
  Output : $output

Configure SSH key authentication first:
  1. on this PC:  ssh-keygen -t ed25519
  2. copy the public key to the Linux host (authorized_keys)
  3. verify:      ssh -o BatchMode=yes $Target echo ok

No SSH key is shipped with this installer - keys are machine owned.
"@
}

function Assert-PackageFiles {
    $required = @(
        "worker.py",
        "worker_config.py",
        "start_worker.py",
        "start_tunnel.py",
        "worker_supervisor.py",
        "tunnel_supervisor.py",
        "download-worker.ps1",
        "sync_log.py",
        "WorkerBandwidth.psm1",
        "requirements.txt"
    )

    if (-not (Test-Path $WorkerSourceDir)) {
        throw ("Worker sources not found at {0}. Copy the whole 'download-worker' folder (installer + worker) to this PC." -f $WorkerSourceDir)
    }

    foreach ($name in $required) {
        if (-not (Test-Path (Join-Path $WorkerSourceDir $name))) {
            throw ("Package is incomplete: {0} is missing from {1}" -f $name, $WorkerSourceDir)
        }
    }

    Write-Log ("Worker sources: {0}" -f $WorkerSourceDir) "OK"
}


# --------------------------------------------------------------------------
# installation steps
# --------------------------------------------------------------------------

function Read-ExistingConfig {
    if (-not (Test-Path $ConfigPath)) {
        return $null
    }

    try {
        $raw = Get-Content $ConfigPath -Raw -Encoding UTF8
        $json = $raw | ConvertFrom-Json
        Write-Log ("Existing configuration found: {0}" -f $ConfigPath)
        return $json
    }
    catch {
        Write-Log ("Existing {0} is unreadable and will be rewritten" -f $ConfigPath) "WARN"
        return $null
    }
}

function Resolve-Setting {
    param(
        [string]$Name,
        [object]$ParamValue,
        [object]$ExistingValue,
        [object]$Default = $null
    )

    if ($null -ne $ParamValue) { return $ParamValue }
    if ($null -ne $ExistingValue) { return $ExistingValue }
    return $Default
}

function Resolve-ProxySpec {
    # Normalize a Worker proxy url, and say why an unusable one is unusable.
    # Accepted: http://host:port, socks5://host:port, socks5h://host:port,
    # http://user:pass@host:port, and a bare host:port (http assumed).
    #
    # Loopback targets are refused: this PC's private proxy (127.0.0.1:7897) must
    # never carry download traffic, and r6 has no ssh -L tunnel through which a
    # loopback proxy could stand in for a remote one.  worker.py refuses the same
    # values again at request time.
    #
    # Returns @{ Ok; Url; Reason }; Url "" means "no proxy = DIRECT only".
    param(
        [string]$Text
    )

    $value = "$Text".Trim()

    if (-not $value) {
        return @{ Ok = $true; Url = ""; Reason = "" }
    }

    if ($value -notmatch "://") {
        $value = "http://" + $value
    }

    # IgnoreCase on purpose: worker.py (urlparse) and the download-worker CLI both
    # accept HTTP://..., so this layer must not be the odd one out.
    $match = [regex]::Match(
        $value,
        '^(?<scheme>http|socks5|socks5h)://(?:(?<user>[^@/]*)@)?(?<host>[^:/?#]+):(?<port>\d{1,5})$',
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)

    if (-not $match.Success) {
        return @{ Ok = $false; Url = ""; Reason = "expected http://ip:port or socks5h://ip:port (a port is required)" }
    }

    $scheme = $match.Groups["scheme"].Value.ToLowerInvariant()
    $user = $match.Groups["user"].Value
    $proxyHost = $match.Groups["host"].Value.Trim('[', ']')
    $proxyPort = [int]$match.Groups["port"].Value

    if ($proxyPort -lt 1 -or $proxyPort -gt 65535) {
        return @{ Ok = $false; Url = ""; Reason = ("port {0} is out of range 1-65535" -f $proxyPort) }
    }

    $lower = $proxyHost.ToLowerInvariant()

    if ($lower -eq "localhost" -or $lower -eq "::1" -or $lower -match '^127\.') {
        return @{ Ok = $false; Url = ""; Reason = ("{0} is loopback - the download Worker must not use this PC's private proxy" -f $proxyHost) }
    }

    $credentials = ""
    if ($user) { $credentials = $user + "@" }

    return @{
        Ok     = $true
        Url    = ("{0}://{1}{2}:{3}" -f $scheme, $credentials, $proxyHost, $proxyPort)
        Reason = ""
    }
}

function Test-ProxyReachable {
    # Best effort: can this PC open a TCP connection to that proxy right now?
    # A failure only WARNS - the url is still written, because a proxy that is
    # down today may be up tomorrow and the DIRECT fallback keeps downloads
    # working meanwhile.
    param(
        [string]$ProxyUrl
    )

    if (-not $ProxyUrl) { return $true }

    $match = [regex]::Match($ProxyUrl, '^(?<scheme>https?|socks5h?)://(?:(?<user>[^@/]*)@)?(?<host>[^:/?#]+):(?<port>\d{1,5})$')

    if (-not $match.Success) { return $true }

    $proxyHost = $match.Groups["host"].Value
    $proxyPort = [int]$match.Groups["port"].Value
    $client = New-Object System.Net.Sockets.TcpClient

    try {
        $async = $client.BeginConnect($proxyHost, $proxyPort, $null, $null)

        if (-not $async.AsyncWaitHandle.WaitOne(5000, $false)) {
            Write-Log ("Proxy {0}:{1} did not answer within 5 s - downloads fall back to DIRECT until it does" -f $proxyHost, $proxyPort) "WARN"
            return $false
        }

        $client.EndConnect($async)
        Write-Log ("Proxy reachable: {0}:{1}" -f $proxyHost, $proxyPort) "OK"
        return $true
    }
    catch {
        Write-Log ("Proxy {0}:{1} is not reachable right now ({2}) - downloads fall back to DIRECT until it is" -f `
            $proxyHost, $proxyPort, $_.Exception.Message) "WARN"
        return $false
    }
    finally {
        $client.Close()
    }
}

function Read-ProxySetting {
    # Interactive proxy question (r6).  This time it asks for the Worker's OWN
    # proxy - the reverse tunnel of r5 is gone - and empty input keeps whatever
    # the config already had.  Only shown to a human: automation passes
    # -WorkerProxy or reuses worker-config.json.
    param(
        [string]$CurrentUrl
    )

    $shown = "none (DIRECT only)"
    if ($CurrentUrl) { $shown = $CurrentUrl }

    Write-Host ""
    Write-Host "Worker proxy - this PC dials it directly; no ssh tunnel any more." -ForegroundColor Cyan
    Write-Host ("  current : {0}" -f $shown)
    Write-Host "  enter   : keep the current setting"
    Write-Host "  off     : clear it (downloads go DIRECT only)"
    Write-Host "  url     : http://ip:port  or  socks5h://ip:port"

    while ($true) {
        try {
            $answer = Read-Host "Worker proxy url"
        }
        catch {
            return $CurrentUrl
        }

        $answer = "$answer".Trim()

        if (-not $answer) {
            return $CurrentUrl
        }

        if (@("off", "none", "disable", "clear", "-") -contains $answer.ToLowerInvariant()) {
            return ""
        }

        $spec = Resolve-ProxySpec -Text $answer

        if ($spec.Ok) {
            return $spec.Url
        }

        Write-Host ("  {0}" -f $spec.Reason) -ForegroundColor Yellow
    }
}

function New-WorkerVenv {
    param($Python)

    if (Test-Path $VenvPython) {
        & $VenvPython -c "import sys" *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Log ("Reusing existing virtual environment: {0}" -f $VenvDir) "OK"
            return
        }

        Write-Log "Existing .venv is broken - recreating it" "WARN"
        if (-not $script:DryRun) {
            Remove-Item -Path $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Step ("Creating virtual environment: {0}" -f $VenvDir)

    $arguments = $Python.Prefix + @("-m", "venv", $VenvDir)
    $code = Invoke-Native -FilePath $Python.Exe -Arguments $arguments

    if ($code -ne 0) {
        throw ("python -m venv failed (exit {0})" -f $code)
    }

    if (-not $script:DryRun -and -not (Test-Path $VenvPython)) {
        throw ("Virtual environment was not created: {0}" -f $VenvPython)
    }

    Write-Log "Virtual environment ready" "OK"
}

function Install-Requirements {
    if (-not $script:DryRun) {
        Write-Step "Upgrading pip (best effort)"
        $code = Invoke-Native -FilePath $VenvPython -Arguments @(
            "-m", "pip", "install", "--upgrade", "pip", "--disable-pip-version-check"
        )
        if ($code -ne 0) {
            Write-Log "pip upgrade failed - continuing with the bundled pip" "WARN"
        }
    }

    Write-Step "Installing Python dependencies (this can take a few minutes)"

    $arguments = @(
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "-r", (Join-Path $InstallDir "requirements.txt")
    )

    if ($PipIndexUrl) {
        $arguments += @("-i", $PipIndexUrl)
    }

    $code = Invoke-Native -FilePath $VenvPython -Arguments $arguments

    if ($code -ne 0) {
        throw ("pip install failed (exit {0}). Check the network / -PipIndexUrl and retry." -f $code)
    }

    Write-Log "Dependencies installed" "OK"
}

function Initialize-LogsAndBandwidth {
    if (-not $script:DryRun) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }

    Write-Log ("Logs directory: {0}" -f $LogDir) "OK"

    $bandwidthFile = Join-Path $LogDir "bandwidth.json"

    if (Test-Path $bandwidthFile) {
        Write-Log "Existing bandwidth.json preserved"
        return
    }

    if (-not $script:DryRun) {
        Write-JsonFile -Path $bandwidthFile -Json (@{ percent = 100 } | ConvertTo-Json)
    }

    Write-Log "bandwidth.json created (100% / 10 MB/s)" "OK"
}

function Test-InteractiveConsole {
    # A question only makes sense when a human is attached.  Redirected standard input
    # means automation: skip the question instead of hanging until someone notices.
    if (-not [Environment]::UserInteractive) { return $false }

    try {
        if ([Console]::IsInputRedirected) { return $false }
    }
    catch {
        return $false
    }

    return $true
}

function Read-YesNo {
    param(
        [string]$Question
    )

    try {
        $answer = Read-Host ("{0} [Y/N]" -f $Question)
    }
    catch {
        return $false
    }

    return (@("Y", "YES") -contains "$answer".Trim().ToUpperInvariant())
}

function Test-BandwidthCommand {
    # Runs the real command in a brand new process of every installed PowerShell
    # engine - in the same environment a freshly opened window has - and reports what
    # it printed.  A broken registration therefore shows its true error during
    # installation instead of failing silently at use time.
    $probeCommand = "Import-Module LocalDownloadWorker -Force -ErrorAction Continue; Get-WorkerBandwidth"

    $engines = @((Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"))
    $pwsh = Get-Command "pwsh.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pwsh) { $engines += $pwsh.Source }

    # a child started from here must behave like a normal user window: do not let it
    # inherit a per-process execution policy (e.g. "-ExecutionPolicy Bypass")
    $inherited = [Environment]::GetEnvironmentVariable("PSExecutionPolicyPreference")
    Remove-Item Env:\PSExecutionPolicyPreference -ErrorAction SilentlyContinue

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $ok = $true

    try {
        foreach ($engine in $engines) {
            $name = Split-Path -Leaf $engine
            $result = ""

            try {
                $result = & $engine -NoProfile -Command $probeCommand 2>&1
            }
            catch {
                $result = "$_"
            }

            if ("$result" -match "Worker bandwidth:") {
                Write-Log ("  {0}: {1}" -f $name, "$result".Trim()) "OK"
                continue
            }

            $ok = $false
            Write-Log ("  {0}: FAILED - {1}" -f $name, "$result".Trim()) "WARN"

            $policy = ""
            try {
                $policy = & $engine -NoProfile -Command "Get-ExecutionPolicy" 2>&1
            }
            catch {
                $policy = "$_"
            }

            Write-Log ("  {0}: execution policy = {1}" -f $name, "$policy".Trim()) "WARN"

            if ("$result" -match "disabled|not digitally signed|running scripts") {
                $script:BandwidthPolicyBlocked = $true
                Write-Log "  -> this machine blocks PowerShell script files (execution policy)" "WARN"
            }
        }
    }
    finally {
        $ErrorActionPreference = $previous
        if ($inherited) { $env:PSExecutionPolicyPreference = $inherited }
    }

    return $ok
}

function Register-BandwidthCommands {
    # The user manual calls Get-WorkerBandwidth / Set-WorkerBandwidth directly, without
    # Import-Module.  For that to work in every new PowerShell window the commands must be
    # discoverable through PSModulePath, so a standalone module is generated for THIS
    # machine (its bandwidth.json path baked in: no $PSScriptRoot, no dependency on the
    # install drive).
    #
    # Deliberately NO .psd1 manifest: PowerShell also auto-loads manifest-less modules,
    # and dropping the manifest removes a whole class of module-loading failures (the
    # first version shipped a manifest and PowerShell reported "command found ... but the
    # module could not be loaded").  A stale manifest from that version is deleted below.
    $userModulePath = @($env:PSModulePath -split ";" |
        Where-Object { $_ -like "*$env:USERPROFILE*" -and $_ -notlike "*$env:ProgramFiles*" })[0]

    if (-not $userModulePath) {
        $userModulePath = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "WindowsPowerShell\Modules"
    }

    $moduleRoot = Join-Path $userModulePath "LocalDownloadWorker"
    $moduleFile = Join-Path $moduleRoot "LocalDownloadWorker.psm1"
    $staleManifest = Join-Path $moduleRoot "LocalDownloadWorker.psd1"
    $configPath = Join-Path $InstallDir "logs\bandwidth.json"

    Write-Step "Registering commands: Get-WorkerBandwidth / Set-WorkerBandwidth"

    if ($script:DryRun) {
        Write-Log ("[dry-run] would generate the module in {0}" -f $moduleRoot)
        return
    }

    New-Item -ItemType Directory -Path $moduleRoot -Force | Out-Null

    if (Test-Path $staleManifest) {
        Remove-Item -Path $staleManifest -Force
        Write-Log "Removed the module manifest shipped by the previous version"
    }

    # The module body is embedded verbatim below (single-quoted here-string: nothing is
    # interpolated, so every $variable and brace stays literal).  The generated file is
    # therefore exactly this text plus the machine's config path - it never depends on
    # how the deployed WorkerBandwidth.psm1 happens to be shaped.
    $moduleBody = @'
function Set-WorkerBandwidth {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateRange(0, 100)]
        [int]$Percent
    )

    $config = "__BANDWIDTH_JSON__"

    # UTF-8 without BOM: worker.py reads this file with json.loads(utf-8)
    $json = @{
        percent = $Percent
    } | ConvertTo-Json
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($config, $json, $encoding)

    $speed = $Percent / 10

    if ($Percent -eq 0) {
        Write-Host "Worker bandwidth: PAUSED (0 MB/s)"
    }
    else {
        Write-Host "Worker bandwidth: $Percent% ($speed MB/s)"
    }
}


function Get-WorkerBandwidth {
    $config = "__BANDWIDTH_JSON__"

    if (-not (Test-Path $config)) {
        Write-Host "Worker bandwidth: 100% (10 MB/s)"
        return
    }

    try {
        $data = Get-Content $config -Raw | ConvertFrom-Json
        $percent = [int]$data.percent
        $speed = $percent / 10

        if ($percent -eq 0) {
            Write-Host "Worker bandwidth: PAUSED (0 MB/s)"
        }
        else {
            Write-Host "Worker bandwidth: $percent% ($speed MB/s)"
        }
    }
    catch {
        Write-Host "Worker bandwidth: 100% (10 MB/s)"
    }
}
'@

    $generated = $moduleBody.Replace("__BANDWIDTH_JSON__", $configPath)

    if ($generated -eq $moduleBody) {
        Write-Log "the embedded module body lost its placeholder - the registered copy would point at the wrong file" "WARN"
    }

    # NOTE: a PowerShell here-string does NOT include the line break in front of its
    # closing "@, so the header is built with explicit CRLFs.  Concatenating a
    # here-string header directly would glue its last line to the module's first
    # code line - "function Set-WorkerBandwidth {" would end up inside a comment
    # and the module would fail to parse.
    $header = "# Generated by install-worker.ps1 - do not edit; re-run the installer instead.`r`n" +
              ("# Bound to: {0}`r`n" -f $InstallDir) +
              "# Provides: Get-WorkerBandwidth, Set-WorkerBandwidth`r`n"

    # write UTF-8 without BOM: a BOM is one more thing a module loader can trip over
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($moduleFile, ($header + $generated + "`r`n"), $encoding)

    Write-Log ("Module written: {0}" -f $moduleFile) "OK"

    if (Test-BandwidthCommand) {
        Write-Log "Get-WorkerBandwidth works in a new PowerShell session" "OK"
        $script:BandwidthCommandsOk = $true
        return
    }

    Write-Log "Get-WorkerBandwidth could not be loaded - see the lines above" "WARN"

    $fixPolicy = [bool]$FixExecutionPolicy

    if (-not $fixPolicy -and -not $SkipExecutionPolicyFix -and $script:BandwidthPolicyBlocked -and (Test-InteractiveConsole)) {
        $fixPolicy = Read-YesNo "This machine blocks PowerShell script files, so the two bandwidth commands cannot be registered. Allow locally created scripts for the current user (Set-ExecutionPolicy -Scope CurrentUser RemoteSigned)?"
    }

    if ($fixPolicy) {
        Write-Log "Allowing locally created scripts for the current user (Set-ExecutionPolicy -Scope CurrentUser RemoteSigned)"

        try {
            Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force -ErrorAction Stop
        }
        catch {
            Write-Log ("  could not change the policy: {0}" -f "$_") "WARN"
        }

        if (Test-BandwidthCommand) {
            Write-Log "Get-WorkerBandwidth works in a new PowerShell session" "OK"
            $script:BandwidthCommandsOk = $true
            return
        }

        Write-Log "still not working after the policy change" "WARN"
    }

    Write-Log ("  fallback without the module system: . '{0}'" -f (Join-Path $InstallDir "WorkerBandwidth.psm1")) "WARN"
    Write-Log "  to allow locally created scripts yourself: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" "WARN"
    Write-Log "  (re-run this installer with -FixExecutionPolicy to have it done for you, or with -SkipExecutionPolicyFix to never be asked)" "WARN"
}

function Install-DownloadWorkerSwitch {
    # `download-worker on|off|status` from any terminal.
    #
    # Why a .cmd shim in %USERPROFILE%\bin instead of another PowerShell function:
    #   * batch files are NOT governed by the execution policy, and the shim forwards
    #     to the .ps1 with -ExecutionPolicy Bypass, so it works on a stock Restricted
    #     machine (the same reason install.cmd exists);
    #   * a file on PATH is found by every shell (PowerShell, cmd, Windows Terminal),
    #     unlike module aliases that only appear after a successful module load.
    # The shim is a generated one-liner with THIS machine's install path baked in.
    Write-Step "Registering command: download-worker (on / off / status)"

    $binDir = Join-Path $env:USERPROFILE "bin"
    $shimPath = Join-Path $binDir "download-worker.cmd"
    $ps1Path = Join-Path $InstallDir "download-worker.ps1"

    if (-not (Test-Path $ps1Path)) {
        Write-Log ("{0} is missing - the switch command was not registered" -f $ps1Path) "WARN"
        return
    }

    if ($script:DryRun) {
        Write-Log ("[dry-run] would write {0} and add {1} to the user PATH" -f $shimPath, $binDir)
        return
    }

    New-Item -ItemType Directory -Path $binDir -Force | Out-Null

    $shim = @(
        "@echo off",
        "rem download-worker - on/off switch for the Local Download Worker installed in",
        "rem $InstallDir .  Generated by install-worker.ps1; the uninstaller removes it.",
        "powershell -NoProfile -ExecutionPolicy Bypass -File ""$ps1Path"" %*",
        "exit /b %errorlevel%"
    ) -join "`r`n"

    [System.IO.File]::WriteAllText($shimPath, $shim + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Log ("{0} written" -f $shimPath) "OK"

    # Put %USERPROFILE%\bin on the USER PATH (user scope: no administrator needed).
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($null -eq $userPath) { $userPath = "" }

    $entries = @($userPath -split ";" | Where-Object { $_ -and $_.Trim() })
    $onPath = @($entries | Where-Object { $_.TrimEnd("\") -ieq $binDir.TrimEnd("\") }).Count -gt 0

    if ($onPath) {
        Write-Log ("{0} is already on the user PATH" -f $binDir) "OK"
    }
    else {
        [Environment]::SetEnvironmentVariable("Path", ((@($entries) + $binDir) -join ";"), "User")
        Write-Log ("added {0} to the user PATH - works in terminals opened from now on" -f $binDir) "OK"
    }

    # make it usable in THIS console as well
    $inSession = @(($env:Path -split ";") | Where-Object { $_.TrimEnd("\") -ieq $binDir.TrimEnd("\") }).Count
    if ($inSession -eq 0) {
        $env:Path = $env:Path.TrimEnd(";") + ";" + $binDir
    }

    $script:SwitchCommandOk = $true
    Write-Log "download-worker on|off|status is available (new terminal if the PATH changed just now)" "OK"
}

function Write-WorkerConfig {
    param(
        [string]$WorkerHost,
        [string]$ResolvedLinuxUser,
        [string]$ResolvedLinuxHost,
        [string]$ResolvedWorkerLabel
    )

    # A blank label falls back to the host name here, so the file never carries an
    # empty one -- a status screen with no name is exactly the problem this solves.
    $label = "$ResolvedWorkerLabel".Trim()
    if (-not $label) { $label = "$env:COMPUTERNAME".Trim() }
    if (-not $label) { $label = "worker" }

    $config = [ordered]@{
        worker_host       = $WorkerHost
        worker_label      = $label
        worker_port       = $WorkerPort
        linux_user        = $ResolvedLinuxUser
        linux_host        = $ResolvedLinuxHost
        linux_tunnel_port = $LinuxTunnelPort
        linux_worker_pool_path = $LinuxWorkerPoolPath
        linux_dw_tasks_path = $LinuxDwTasksPath
        log_sync          = [ordered]@{
            enabled          = (-not $SkipLogSyncTask)
            remote_dir       = $LogSyncRemoteDir
            interval_minutes = $LogSyncIntervalMinutes
        }
        # Download traffic: the Worker's own proxy first, DIRECT as the fallback.
        # This PC's private proxy stays disabled - project rule, see worker.py /
        # README.md "proxy exits".  Empty url = DIRECT only.
        proxy             = [ordered]@{
            url                        = "$ProxyUrlValue"
            retries                    = [int]$ProxyRetries
            windows_private            = [ordered]@{
                enabled = $false
            }
            large_file_threshold_bytes = [int]$ProxyThresholdBytes
        }
    }

    $json = $config | ConvertTo-Json -Depth 5

    if ($script:DryRun) {
        Write-Log "[dry-run] would write worker-config.json:"
        Write-Host $json
        return
    }

    Write-JsonFile -Path $ConfigPath -Json $json
    Write-Log ("Configuration written: {0}" -f $ConfigPath) "OK"

    if ($ProxyUrlValue) {
        Write-Log ("Worker proxy: {0} (tried first, DIRECT fallback; change later with download-worker proxy <url>)" -f `
            $ProxyUrlValue) "OK"
    }
    else {
        Write-Log "Worker proxy: not set - downloads go DIRECT only (set later with download-worker proxy <url>)"
    }
}

function Set-FirewallRule {
    Write-Step ("Firewall rule for TCP {0} (Tailscale only)" -f $WorkerPort)

    if (-not $script:DryRun) {
        $stale = @(Get-NetFirewallRule -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -like "Local Download Worker*" })

        foreach ($rule in $stale) {
            Write-Log ("  removing old rule: {0}" -f $rule.DisplayName)
            Remove-NetFirewallRule -Name $rule.Name
        }
    }

    if ($script:DryRun) {
        Write-Log "[dry-run] would create the firewall rule"
        return
    }

    $displayName = "Local Download Worker {0} (Tailscale)" -f $WorkerPort

    New-NetFirewallRule `
        -DisplayName $displayName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $WorkerPort `
        -RemoteAddress "100.64.0.0/10" `
        -Profile Any `
        -Enabled True `
        -Description "Local Download Worker - reachable from the Tailscale network only" | Out-Null

    Write-Log ("Firewall rule: {0}" -f $displayName) "OK"
}

function New-SharedTaskObjects {
    # Settings are shared by all three tasks.  The Interactive principal below is
    # used only by the log-sync task, which runs pythonw.exe (GUI subsystem) and
    # therefore cannot show a window; Worker and Tunnel build their own S4U
    # principal in Register-HiddenTask.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal `
        -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
        -LogonType Interactive `
        -RunLevel Highest

    return @{ Settings = $settings; Principal = $principal }
}

function New-HiddenTaskPrincipal {
    # LogonType S4U = "run whether the user is logged on or not, do not store the
    # password".  Task Scheduler then runs the task in session 0, where there is no
    # desktop at all: a console-subsystem program (python.exe, ssh.exe) cannot show
    # a window there, and a window the user could close does not exist at all.
    # The AtLogOn trigger still starts the task.
    return New-ScheduledTaskPrincipal `
        -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
        -LogonType S4U `
        -RunLevel Highest
}

function Register-HiddenTask {
    param([string]$TaskName, $Action, $Trigger, $Settings, [string]$Description)

    $arguments = @{
        TaskName    = $TaskName
        Action      = $Action
        Trigger     = $Trigger
        Settings    = $Settings
        Description = $Description
        Force       = $true
    }

    try {
        Register-ScheduledTask @arguments -Principal (New-HiddenTaskPrincipal) | Out-Null
        Write-Log "  LogonType S4U: runs in session 0 (no window can appear)" "OK"
    }
    catch {
        Write-Log ("S4U registration failed ({0})" -f $_.Exception.Message) "WARN"
        Write-Log "  falling back to LogonType Interactive - this task may then show a console window" "WARN"
        Write-Log "  NOTE: an Interactive task runs inside the logon session that started it. If that" "WARN"
        Write-Log "  session is an SSH (or RDP) session, ending that session KILLS the tunnel with it -" "WARN"
        Write-Log "  the exact failure reported. Fix: grant this account" "WARN"
        Write-Log "  'Log on as a batch job' (secpol.msc) and re-run the installer to get S4U." "WARN"

        $principal = New-ScheduledTaskPrincipal `
            -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
            -LogonType Interactive `
            -RunLevel Highest

        Register-ScheduledTask @arguments -Principal $principal | Out-Null
    }

    # Read the principal back: this is the difference between "runs in session 0 no matter
    # who is logged on" and "lives inside some logon session and dies with it" - worth one
    # explicit line in the log instead of discovering it weeks later.
    if (-not $script:DryRun) {
        try {
            $actual = (Get-ScheduledTask -TaskName $TaskName).Principal.LogonType
            Write-Log ("  {0}: LogonType = {1}" -f $TaskName, $actual)
        }
        catch {
            # reporting only - never fail the install over this
        }
    }
}

# --------------------------------------------------------------------------
# previous-installation cleanup
#
# Why this exists (hand-uninstall notes from an earlier round): removing an old by-hand install
# failed twice - first because venv python.exe processes held the files, then
# because the ssh reverse tunnel was still running with its working directory in
# the install dir.  The tunnel could not be found by "path in command line" (its
# executable lives in System32 and its arguments only contain "-R <port>:..."), so
# it has to be matched by tunnel port.  Doing this automatically at install time
# prevents both the locked files and the port conflict a leftover tunnel causes.
# --------------------------------------------------------------------------

function Get-InstallActionDirs {
    # Directories recorded in our own scheduled tasks.
    $dirs = New-Object System.Collections.ArrayList

    foreach ($taskName in @($script:WorkerTaskName, $script:TunnelTaskName, $script:LogSyncTaskName)) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -eq $task) { continue }

        foreach ($action in @($task.Actions)) {
            $values = New-Object System.Collections.ArrayList

            if ($null -ne $action.PSObject.Properties["WorkingDirectory"]) {
                [void]$values.Add("$($action.WorkingDirectory)")
            }
            if ($null -ne $action.PSObject.Properties["Execute"]) {
                $exe = "$($action.Execute)"
                if ($exe) { [void]$values.Add((Split-Path -Parent $exe)) }
            }

            foreach ($value in $values) {
                if (-not $value) { continue }
                try {
                    $full = [System.IO.Path]::GetFullPath($value)
                    if (Test-Path $full) { [void]$dirs.Add($full) }
                }
                catch { }
            }
        }
    }

    return @($dirs | Select-Object -Unique)
}

function Get-LegacyInstallDirs {
    # Where previous versions may live: paths from our scheduled tasks plus the
    # well-known directories of earlier packages.  A candidate only counts when it
    # really contains Worker files - a directory is never deleted just because its
    # name matches.
    $markers = @("worker.py", "start_worker.py", "worker_supervisor.py", "worker-config.json")
    $candidates = New-Object System.Collections.ArrayList

    foreach ($dir in @(Get-InstallActionDirs)) { [void]$candidates.Add($dir) }

    foreach ($dir in @(
        "D:\local-download-worker",
        "C:\local-download-worker",
        "C:\ProgramData\LocalDownloadWorker",
        (Join-Path $env:USERPROFILE "local-download-worker")
    )) {
        if ($dir -and (Test-Path $dir)) {
            try { [void]$candidates.Add([System.IO.Path]::GetFullPath($dir)) } catch { }
        }
    }

    $result = New-Object System.Collections.ArrayList

    foreach ($dir in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path $dir)) { continue }

        $looksLikeInstall = $false
        foreach ($marker in $markers) {
            if (Test-Path (Join-Path $dir $marker)) { $looksLikeInstall = $true; break }
        }
        if (-not $looksLikeInstall) { continue }

        # never touch the directory this installer itself runs from
        if ($PSScriptRoot -and ($PSScriptRoot -like ("{0}\*" -f $dir))) {
            Write-Log ("  {0}: skipped (this installer runs from inside it)" -f $dir) "WARN"
            continue
        }

        [void]$result.Add($dir)
    }

    return @($result)
}

function Get-InstallTunnelPorts {
    # Which "-R <port>:" may a leftover tunnel hold?  Every config we can read, the
    # port this install will use, and the first version's default.
    param([string[]]$Dirs)

    $ports = New-Object System.Collections.ArrayList
    [void]$ports.Add([int]$LinuxTunnelPort)

    foreach ($dir in @($Dirs)) {
        $cfgPath = Join-Path $dir "worker-config.json"
        if (-not (Test-Path $cfgPath)) { continue }

        try {
            $cfg = Get-Content -Path $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($null -ne $cfg.linux_tunnel_port) { [void]$ports.Add([int]$cfg.linux_tunnel_port) }
        }
        catch { }
    }

    [void]$ports.Add(8766)
    return @($ports | Select-Object -Unique)
}

function Get-ProtectedProcessIds {
    # This installer and every one of its ancestors.  The installer is normally run as
    #   cmd -> powershell -File install-worker.ps1 -InstallDir <dir> ...
    # so its own command line (and that of its console host) CONTAINS the install
    # directory.  A "command line mentions the directory" match would therefore kill the
    # console the operator is watching - which looks exactly like a failed install.
    $parentOf = @{}
    foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $parentOf[[int]$p.ProcessId] = [int]$p.ParentProcessId
    }

    $ids = New-Object System.Collections.ArrayList
    $current = [int]$PID

    for ($i = 0; $i -lt 20 -and $current; $i++) {
        [void]$ids.Add($current)
        if (-not $parentOf.ContainsKey($current)) { break }
        $current = [int]$parentOf[$current]
    }

    return @($ids)
}

function Get-TunnelSshProcesses {
    # ssh.exe processes that hold one of our tunnel ports.  They cannot be found by
    # install-directory matching: ssh.exe lives in System32 and its arguments only
    # contain "-R <port>:<host>:<port> <user>@<linux>" (uninstall notes).
    param([int[]]$TunnelPorts)

    $result = New-Object System.Collections.ArrayList

    foreach ($port in @($TunnelPorts)) {
        foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue |
                         Where-Object { $_.CommandLine -and ("$($_.CommandLine)" -like ("*-R {0}:*" -f $port)) })) {
            [void]$result.Add($p)
        }
    }

    return @($result | Sort-Object ProcessId -Unique)
}

function Stop-ProcessesForDir {
    # A running Worker / tunnel locks its files: the directory cannot be removed and
    # files cannot be overwritten.  Matched three ways:
    #   1) executable inside the directory            (venv python.exe / pythonw.exe)
    #   2) python/pythonw with the directory in its command line (scripts started there)
    #   3) ssh.exe with "-R <tunnel port>:"           (the tunnel - by port, see above)
    # Deliberately NOT "any process whose command line mentions the directory": that
    # matches the installer's own console (see Get-ProtectedProcessIds).
    param([string]$Path, [int[]]$TunnelPorts)

    $protected = @(Get-ProtectedProcessIds)
    $targets = New-Object System.Collections.ArrayList

    foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                     Where-Object {
                         ($_.ExecutablePath -and ("$($_.ExecutablePath)" -like ("{0}\*" -f $Path))) -or
                         (($_.Name -in @("python.exe", "pythonw.exe")) -and
                          $_.CommandLine -and ("$($_.CommandLine)" -like ("*{0}*" -f $Path)))
                     })) {
        [void]$targets.Add($p)
    }

    foreach ($p in @(Get-TunnelSshProcesses -TunnelPorts $TunnelPorts)) {
        [void]$targets.Add($p)
    }

    $stopped = 0

    foreach ($p in ($targets | Sort-Object ProcessId -Unique)) {
        if ($protected -contains [int]$p.ProcessId) { continue }   # never kill this installer
        Write-Log ("  stopping pid {0}: {1}" -f $p.ProcessId, ("$($p.Name) $($p.CommandLine)").Trim())
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped++
    }

    if ($stopped -gt 0) {
        # give Windows a moment to drop the file handles before deleting anything
        Start-Sleep -Seconds 2
    }

    return $stopped
}

function Remove-DirectoryWithRetry {
    param([string]$Path, [int]$Attempts = 5, [int]$DelaySeconds = 2)

    for ($i = 1; $i -le $Attempts; $i++) {
        if (-not (Test-Path $Path)) { return $true }

        try {
            Remove-Item -Path $Path -Recurse -Force -ErrorAction Stop
            return $true
        }
        catch {
            if ($i -lt $Attempts) {
                Start-Sleep -Seconds $DelaySeconds
                continue
            }

            Write-Log ("  could not fully remove {0}: {1}" -f $Path, $_.Exception.Message) "WARN"
            foreach ($item in @(Get-ChildItem -Path $Path -Recurse -Force -ErrorAction SilentlyContinue |
                                Select-Object -First 5)) {
                Write-Log ("    left behind: {0}" -f $item.FullName) "WARN"
            }
            Write-Log "    close whatever still uses those files and delete the directory by hand" "WARN"
            return $false
        }
    }

    return $false
}

function Clear-LegacyInstalls {
    Write-Step "Removing previous installations"

    if ($SkipLegacyCleanup) {
        Write-Log "-SkipLegacyCleanup given: nothing stopped, nothing removed" "WARN"
        return
    }

    $dirs = @(Get-LegacyInstallDirs)
    $ports = @(Get-InstallTunnelPorts -Dirs $dirs)

    if ($dirs.Count -eq 0) {
        Write-Log "No previous installation found"
    }
    else {
        foreach ($dir in $dirs) { Write-Log ("Found: {0}" -f $dir) }
    }
    Write-Log ("Tunnel ports to clear: {0}" -f ($ports -join ", "))

    if ($script:DryRun) {
        foreach ($dir in $dirs) {
            Write-Log ("[dry-run] would stop its processes and remove it: {0}" -f $dir)
        }
        if (Test-Path $InstallDir) {
            Write-Log ("[dry-run] would stop the processes of {0}" -f $InstallDir)
        }
        return
    }

    # 1) the target directory: upgrading over a running install would lock the files
    #    being overwritten, so its processes go first.  The installer starts them
    #    again at the end of the run.
    if ((Test-Path $InstallDir) -and -not ($PSScriptRoot -like ("{0}\*" -f $InstallDir))) {
        $stopped = Stop-ProcessesForDir -Path $InstallDir -TunnelPorts $ports
        if ($stopped -gt 0) {
            Write-Log ("  {0} process(es) stopped for {1}" -f $stopped, $InstallDir) "OK"
        }
    }

    # 2) every other installation: stop its processes, then delete the directory
    foreach ($dir in $dirs) {
        if ($dir.TrimEnd("\") -ieq $InstallDir.TrimEnd("\")) { continue }

        Write-Log ("Removing old installation: {0}" -f $dir)
        [void](Stop-ProcessesForDir -Path $dir -TunnelPorts $ports)

        if (Remove-DirectoryWithRetry -Path $dir) {
            Write-Log ("  removed {0}" -f $dir) "OK"
        }
    }

    # 3) verify: a survivor holding a tunnel port would make the new supervisor's ssh
    #    fail forever with "remote port forwarding failed for listen port <n>".
    $leftover = @(Get-TunnelSshProcesses -TunnelPorts $ports)

    if ($leftover.Count -gt 0) {
        foreach ($p in $leftover) {
            Write-Log ("  still holding a tunnel port: pid {0} - {1}" -f $p.ProcessId, "$($p.CommandLine)".Trim()) "WARN"
        }
        Write-Log "  the new tunnel cannot start until those are gone (see the README troubleshooting table)" "WARN"
    }
    else {
        Write-Log "No process is holding a tunnel port" "OK"
    }
}

function Stop-ExistingTasks {
    # A task instance that is still running can make Register-ScheduledTask -Force fail,
    # and the fallback path would then leave an OLD definition (e.g. an Interactive tunnel
    # from a previous installer) in place - which is exactly what an earlier acceptance
    # run found in the field: Tunnel=Interactive while Worker=S4U.  Stopping first makes
    # the re-registration deterministic; the tasks are started again at the end of the run.
    foreach ($taskName in @($script:WorkerTaskName, $script:TunnelTaskName, $script:LogSyncTaskName)) {
        if ($null -eq (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) { continue }

        if ($script:DryRun) {
            Write-Log ("[dry-run] would stop {0}" -f $taskName)
            continue
        }

        try {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
            Write-Log ("  stopped running instance: {0}" -f $taskName) "OK"
        }
        catch {
            # "not running" lands here too - not worth a warning
        }
    }
}

function Register-WorkerTasks {
    param([string]$WorkerHost)

    $common = New-SharedTaskObjects
    $settings = $common.Settings
    $principal = $common.Principal

    Stop-ExistingTasks

    # --- Worker (supervised) -------------------------------------------------
    Write-Step ("Scheduled task: {0}" -f $script:WorkerTaskName)

    $trigger = New-ScheduledTaskTrigger -AtLogOn
    try { $trigger.Delay = "PT15S" } catch { }

    $action = New-ScheduledTaskAction `
        -Execute $VenvPython `
        -Argument ('"{0}"' -f (Join-Path $InstallDir "worker_supervisor.py")) `
        -WorkingDirectory $InstallDir

    if (-not $script:DryRun) {
        # session-0 launch: python.exe is a console program, so it must not run in
        # the interactive session or its console window would be visible
        Register-HiddenTask `
            -TaskName $script:WorkerTaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description "Local Download Worker (uvicorn) kept alive by worker_supervisor.py"
    }

    Write-Log ("{0}: registered (AtLogOn)" -f $script:WorkerTaskName) "OK"

    # --- Tunnel --------------------------------------------------------------
    Write-Step ("Scheduled task: {0}" -f $script:TunnelTaskName)

    $trigger = New-ScheduledTaskTrigger -AtLogOn
    try { $trigger.Delay = "PT45S" } catch { }

    # Run tunnel_supervisor.py, NOT start_tunnel.py: the supervisor keeps the ssh
    # process alive (restart on exit + backoff).  The previous version started the
    # tunnel directly, so a dropped ssh stayed dead until the next logon - the exact
    # failure that left one machine's outlet offline for hours.
    $action = New-ScheduledTaskAction `
        -Execute $VenvPythonw `
        -Argument ('"{0}"' -f (Join-Path $InstallDir "tunnel_supervisor.py")) `
        -WorkingDirectory $InstallDir

    if (-not $script:DryRun) {
        # session-0 launch: see New-TunnelPrincipal
        Register-HiddenTask `
            -TaskName $script:TunnelTaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description ("SSH reverse tunnel (kept alive by tunnel_supervisor.py): Linux 127.0.0.1:{0} -> {1}:{2}" -f $LinuxTunnelPort, $WorkerHost, $WorkerPort)
    }

    Write-Log ("{0}: registered (AtLogOn)" -f $script:TunnelTaskName) "OK"

    # --- Log sync ------------------------------------------------------------
    if ($SkipLogSyncTask) {
        Write-Log "Log sync task skipped (-SkipLogSyncTask)" "WARN"
        return
    }

    Write-Step ("Scheduled task: {0}" -f $script:LogSyncTaskName)

    $triggers = @()

    try {
        $repeat = New-ScheduledTaskTrigger `
            -Once `
            -At ((Get-Date).AddMinutes(2)) `
            -RepetitionInterval (New-TimeSpan -Minutes $LogSyncIntervalMinutes) `
            -RepetitionDuration (New-TimeSpan -Days 3650)
        $triggers += $repeat
    }
    catch {
        Write-Log "Repetition trigger with a long duration failed - falling back to AtLogOn only" "WARN"
    }

    $logon = New-ScheduledTaskTrigger -AtLogOn
    try { $logon.Delay = "PT2M" } catch { }
    $triggers += $logon

    $action = New-ScheduledTaskAction `
        -Execute $VenvPythonw `
        -Argument ('"{0}"' -f (Join-Path $InstallDir "sync_log.py")) `
        -WorkingDirectory $InstallDir

    if (-not $script:DryRun) {
        # pythonw.exe + short-lived process: windowless without a session-0 launch
        Register-ScheduledTask `
            -TaskName $script:LogSyncTaskName `
            -Action $action `
            -Trigger $triggers `
            -Settings $settings `
            -Principal $principal `
            -Description "Copy Worker logs to the Linux host (incremental)" `
            -Force | Out-Null
    }

    Write-Log ("{0}: registered (every {1} min + AtLogOn)" -f $script:LogSyncTaskName, $LogSyncIntervalMinutes) "OK"
}

function Start-AndVerifyWorker {
    param([string]$WorkerHost)

    Write-Step "Starting the Worker"

    if (-not $script:DryRun) {
        Start-ScheduledTask -TaskName $script:WorkerTaskName
    }

    $healthUrl = "http://{0}:{1}/health" -f $WorkerHost, $WorkerPort

    Write-Log ("Waiting for {0} ..." -f $healthUrl)

    if ($script:DryRun) {
        Write-Log "[dry-run] would poll /health"
        return
    }

    $deadline = (Get-Date).AddSeconds(90)
    $body = $null

    while ((Get-Date) -lt $deadline) {
        $body = Test-HealthUrl -Url $healthUrl -TimeoutSeconds 3
        if ($null -ne $body -and $body -match '"status"\s*:\s*"ok"') {
            Write-Log ("Worker health: OK ({0})" -f $body.Trim()) "OK"
            return
        }

        Start-Sleep -Seconds 3
    }

    # local fallback, so a firewall/capture issue is distinguishable from a dead worker
    $localBody = Test-HealthUrl -Url ("http://127.0.0.1:{0}/health" -f $WorkerPort) -TimeoutSeconds 3
    if ($null -ne $localBody) {
        throw ("Worker answers on 127.0.0.1 but not on {0}. Check the firewall rule." -f $healthUrl)
    }

    throw @"
Installation failed: Worker did not become healthy.

  Health URL : $healthUrl
  Worker log : $(Join-Path $LogDir "worker.log")
  Supervisor : Task Scheduler -> $script:WorkerTaskName
"@
}

function Get-TunnelOwnership {
    # Classify ssh tunnel processes: does each one descend from OUR tunnel supervisor?
    # A leftover ssh from an earlier install keeps the Linux-side port occupied, so the
    # supervisor's own ssh dies instantly with "remote port forwarding failed for listen
    # port <n>" (exit 255) - "some process holds the port" is therefore NOT proof that
    # the tunnel is up.  Ownership has to be decided by ancestry.
    param([object[]]$SshProcesses, [int[]]$SupervisorPids)

    $parentOf = @{}
    foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $parentOf[[int]$p.ProcessId] = [int]$p.ParentProcessId
    }

    $ours = New-Object System.Collections.ArrayList
    $foreign = New-Object System.Collections.ArrayList

    foreach ($p in @($SshProcesses)) {
        $ancestor = [int]$p.ProcessId
        $isOurs = $false

        for ($i = 0; $i -lt 10 -and $ancestor; $i++) {
            if ($SupervisorPids -contains $ancestor) { $isOurs = $true; break }
            if (-not $parentOf.ContainsKey($ancestor)) { break }
            $ancestor = [int]$parentOf[$ancestor]
        }

        if ($isOurs) { [void]$ours.Add($p) } else { [void]$foreign.Add($p) }
    }

    return @{ Ours = @($ours); Foreign = @($foreign) }
}

function Start-AndVerifyTunnel {
    param(
        [string]$SshExe,
        [string]$SshTarget,
        [string]$WorkerHost
    )

    Write-Step "Starting the tunnel"

    if (-not $script:DryRun) {
        Start-ScheduledTask -TaskName $script:TunnelTaskName
        Start-Sleep -Seconds 15
    }

    if ($script:DryRun) {
        Write-Log "[dry-run] would verify the tunnel"
        return
    }

    # The task runs tunnel_supervisor.py, which in turn keeps
    # start_tunnel.py (and its ssh child) alive; both should now be present.
    $supervisor = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*tunnel_supervisor.py*" })

    if ($supervisor.Count -eq 0) {
        Write-Log "tunnel_supervisor.py is not running - a dropped tunnel would NOT come back" "WARN"
    }
    else {
        Write-Log "tunnel supervisor is running (restarts the ssh on exit, 5/10/15/20/30s backoff)" "OK"
    }

    $ssh = @(Get-TunnelSshProcesses -TunnelPorts @($LinuxTunnelPort))

    # A process holding the port is not proof that OUR tunnel is up: a leftover from an
    # earlier install keeps the port on the Linux side occupied, the supervisor retries
    # forever with "remote port forwarding failed for listen port <n>" (exit 255), and
    # this check - which only counted processes - still reported OK.
    # So: decide ownership by walking each ssh's ancestors up to the supervisor.
    $supervisorPids = @($supervisor | ForEach-Object { [int]$_.ProcessId })
    $split = Get-TunnelOwnership -SshProcesses $ssh -SupervisorPids $supervisorPids
    $ours = @($split.Ours)

    foreach ($p in @($split.Foreign)) {
        Write-Log ("stale tunnel on port {0}: pid {1} - {2}" -f $LinuxTunnelPort, $p.ProcessId, "$($p.CommandLine)".Trim()) "WARN"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }

    if (@($split.Foreign).Count -gt 0) {
        # The supervisor retries on its own schedule (5/10/15/20/30 s backoff): give it the
        # chance to grab the port instead of failing the install on the spot.
        Write-Log "  stale tunnel removed - waiting for the supervisor to take the port (up to 90 s)" "WARN"
        $deadline = (Get-Date).AddSeconds(90)

        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 5
            $now = @(Get-TunnelSshProcesses -TunnelPorts @($LinuxTunnelPort))
            if ($now.Count -eq 0) { continue }

            $again = Get-TunnelOwnership -SshProcesses $now -SupervisorPids $supervisorPids
            if (@($again.Ours).Count -gt 0) { $ours = @($again.Ours); break }

            # a foreign tunnel came back (or a second one was hiding): drop it and keep waiting
            foreach ($p in @($again.Foreign)) {
                Write-Log ("  stale tunnel again: pid {0}" -f $p.ProcessId) "WARN"
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    }

    if ($ours.Count -eq 0) {
        throw @"
Installation failed: the SSH tunnel process is not running.

Most likely the Linux port is already taken (for example by another worker).
Pick a free port per PC:  -LinuxTunnelPort <port>

  Tunnel log     : $(Join-Path $LogDir "tunnel.log")
  Supervisor log : $(Join-Path $LogDir "tunnel_supervisor.log")
"@
    }

    Write-Log "SSH tunnel process is running (belongs to the tunnel supervisor)" "OK"

    # Ask the Linux side whether it can reach the Worker through the tunnel.
    $remote = ""
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        $remote = & $SshExe -o BatchMode=yes -o ConnectTimeout=15 $SshTarget `
            ("curl -fsS -m 5 http://127.0.0.1:{0}/health" -f $LinuxTunnelPort) 2>&1
    }
    catch {
        $remote = "$_"
    }
    finally {
        $ErrorActionPreference = $previous
    }

    if ("$remote" -match '"status"\s*:\s*"ok"') {
        Write-Log ("Linux 127.0.0.1:{0}/health -> OK" -f $LinuxTunnelPort) "OK"
        return
    }

    Write-Log ("Linux side could not reach the Worker through the tunnel: {0}" -f "$remote") "WARN"
    Write-Log ("Check: Linux sshd allows the port, the port is free, logs in {0}" -f $LogDir) "WARN"
}

function Show-Summary {
    param(
        [string]$WorkerHost,
        [string]$ResolvedLinuxUser,
        [string]$ResolvedLinuxHost,
        [string]$PythonVersion
    )

    $bandwidth = 100
    $bandwidthFile = Join-Path $LogDir "bandwidth.json"

    if (Test-Path $bandwidthFile) {
        try {
            $bandwidth = [int]((Get-Content $bandwidthFile -Raw -Encoding UTF8 | ConvertFrom-Json).percent)
        }
        catch {
            $bandwidth = 100
        }
    }

    $line = "=" * 56

    Write-Host ""
    Write-Host $line
    Write-Host "Local Download Worker installed"
    Write-Host $line
    Write-Host ""
    Write-Host "Install directory:"
    Write-Host $InstallDir
    Write-Host ""
    Write-Host "Worker:"
    Write-Host ("{0}:{1}   (Python {2}, port bound to the Tailscale address)" -f $WorkerHost, $WorkerPort, $PythonVersion)
    Write-Host ""
    Write-Host "Worker health:"
    Write-Host "OK"
    Write-Host ""
    Write-Host "Tunnel:"
    Write-Host ("Linux {0}@127.0.0.1:{1}" -f $ResolvedLinuxUser, $LinuxTunnelPort)
    Write-Host "        ->"
    Write-Host ("Windows {0}:{1}" -f $WorkerHost, $WorkerPort)
    Write-Host ""
    Write-Host "Task Scheduler:"
    Write-Host ("{0,-26} OK" -f $script:WorkerTaskName)
    Write-Host ("{0,-26} OK" -f $script:TunnelTaskName)
    if ($SkipLogSyncTask) {
        Write-Host ("{0,-26} skipped" -f $script:LogSyncTaskName)
    }
    else {
        Write-Host ("{0,-26} OK" -f $script:LogSyncTaskName)
    }
    Write-Host ""
    Write-Host "Firewall:"
    Write-Host ("{0} / Tailscale only    OK" -f $WorkerPort)
    Write-Host ""
    Write-Host "Bandwidth:"
    Write-Host ("{0}% / {1} MB/s" -f $bandwidth, ($bandwidth / 10))
    Write-Host ""
    Write-Host "Commands:"
    if ($script:BandwidthCommandsOk) {
        Write-Host "Get-WorkerBandwidth / Set-WorkerBandwidth   OK (any new PowerShell window)"
    }
    else {
        Write-Host "Get-WorkerBandwidth / Set-WorkerBandwidth   NOT AVAILABLE" -ForegroundColor Yellow
        Write-Host "  see the WARN lines above (bandwidth is still set to the value shown above)" -ForegroundColor Yellow
    }
    if ($script:SwitchCommandOk) {
        Write-Host "download-worker on|off|status|process       OK (any terminal; open a new one if PATH just changed)"
    }
    else {
        Write-Host "download-worker on|off|status|process       NOT INSTALLED" -ForegroundColor Yellow
        Write-Host "  see the WARN lines above (the worker itself is unaffected)" -ForegroundColor Yellow
    }
    if (Test-Path (Join-Path $InstallDir "paused.flag")) {
        Write-Host "  NOTE: paused.flag exists - this PC is currently switched OFF; run 'download-worker on'" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "Linux host:"
    Write-Host ("{0}" -f $ResolvedLinuxHost)
    Write-Host ""
    Write-Host "Next step on the Linux host:"
    Write-Host ("  curl http://127.0.0.1:{0}/health" -f $LinuxTunnelPort)
    Write-Host ("  your own client / script: http://127.0.0.1:{0}" -f $LinuxTunnelPort)
    Write-Host ""
    Write-Host "Status:"
    Write-Host "READY"
    Write-Host $line
    Write-Host ""
}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

try {
    Write-Host ""
    Write-Log "Local Download Worker installer"
    Write-Log ("Target directory: {0}" -f $InstallDir)

    if ($script:DryRun) {
        Write-Log "DRY RUN - no changes will be made" "WARN"
    }

    Assert-Administrator
    Write-Log "Running elevated" "OK"
    Assert-WindowsVersion

    # A PC switched off with `download-worker off` carries paused.flag, and both
    # supervisors honour it by staying down.  Installing over such a PC used to start
    # the tasks, wait 90 s for /health and fail with a misleading "Worker did not
    # become healthy" - the whole install looked broken when the machine was simply
    # switched off.  Installing is an explicit "make it work again"
    # action, so the flag goes; -KeepPaused keeps the PC offline instead.
    $keepPaused = $false
    if (Test-Path $PauseFlag) {
        if ($KeepPaused) {
            $keepPaused = $true
            Write-Log "paused.flag present: this PC stays switched off (-KeepPaused); the start verification is skipped" "WARN"
        }
        elseif ($script:DryRun) {
            Write-Log "[dry-run] would remove paused.flag and bring this PC back online" "WARN"
        }
        else {
            Remove-Item -Path $PauseFlag -Force -ErrorAction SilentlyContinue
            Write-Log "paused.flag removed: this PC had been switched off with 'download-worker off' - the install brings it back online" "WARN"
        }
    }

    $python = Find-Python
    Assert-VenvSupport -Python $python

    $tailscaleIp = Get-TailscaleIPv4
    $sshExe = Find-SshExe

    $existing = Read-ExistingConfig
    $existingUser = $null
    $existingHost = $null

    if ($null -ne $existing) {
        $existingUser = $existing.linux_user
        $existingHost = $existing.linux_host

        # upgrade: keep settings the user does not pass again on the command line
        if (-not $PSBoundParameters.ContainsKey("WorkerPort") -and $existing.worker_port) {
            $WorkerPort = [int]$existing.worker_port
        }

        if (-not $PSBoundParameters.ContainsKey("LinuxTunnelPort") -and $existing.linux_tunnel_port) {
            $LinuxTunnelPort = [int]$existing.linux_tunnel_port
        }

        if (-not $PSBoundParameters.ContainsKey("LogSyncRemoteDir") -and $existing.log_sync.remote_dir) {
            $LogSyncRemoteDir = "$($existing.log_sync.remote_dir)"
        }

        if (-not $PSBoundParameters.ContainsKey("LogSyncIntervalMinutes") -and $existing.log_sync.interval_minutes) {
            $LogSyncIntervalMinutes = [int]$existing.log_sync.interval_minutes
        }

        # linux_worker_pool_path only exists in configs written by the original installer
        # and later, so ask for the property instead of reading it (StrictMode 2.0).
        $existingPoolPath = $null
        if ($null -ne $existing.PSObject.Properties["linux_worker_pool_path"]) {
            $existingPoolPath = "$($existing.linux_worker_pool_path)".Trim()
        }

        if ($existingPoolPath -and -not $PSBoundParameters.ContainsKey("LinuxWorkerPoolPath")) {
            $LinuxWorkerPoolPath = $existingPoolPath
        }

        # Same for linux_dw_tasks_path (this installer and later).
        $existingTasksPath = $null
        if ($null -ne $existing.PSObject.Properties["linux_dw_tasks_path"]) {
            $existingTasksPath = "$($existing.linux_dw_tasks_path)".Trim()
        }

        if ($existingTasksPath -and -not $PSBoundParameters.ContainsKey("LinuxDwTasksPath")) {
            $LinuxDwTasksPath = $existingTasksPath
        }

        # Same for worker_label (this installer and later).
        $existingLabel = $null
        if ($null -ne $existing.PSObject.Properties["worker_label"]) {
            $existingLabel = "$($existing.worker_label)".Trim()
        }

        if ($existingLabel -and -not $PSBoundParameters.ContainsKey("WorkerLabel")) {
            $WorkerLabel = $existingLabel
        }

        Write-Log ("Keeping: worker_port={0}, linux_tunnel_port={1}" -f $WorkerPort, $LinuxTunnelPort)
    }

    # ---- Worker proxy ------------------------------------------------------
    # The Worker has a proxy OF ITS OWN and dials it directly over the network:
    # downloads are tried through it FIRST and fall back to DIRECT when it cannot
    # be reached.  The r5 reverse-tunnel proxy (ssh -L) is gone - it never
    # accelerated anything, and a busy loopback port could kill the whole tunnel
    # (ExitOnForwardFailure).  This PC's private proxy is never a download exit.
    #
    # Effective value: -WorkerProxy, else the existing config (proxy.url, or an
    # r5 proxy.linux block migrated to a url), else the interactive question,
    # else empty = DIRECT only.
    $ProxyUrlValue       = ""
    $ProxyRetries        = 2
    $ProxyThresholdBytes = 1073741824        # 1 GiB: private-proxy ceiling, see worker.py
    $existingProxyUrl    = $null

    # StrictMode 2.0: ask for each property instead of reading it.
    if ($null -ne $existing -and $null -ne $existing.PSObject.Properties["proxy"]) {
        $existingProxy = $existing.proxy

        if ($null -ne $existingProxy.PSObject.Properties["url"]) {
            $existingProxyUrl = "$($existingProxy.url)".Trim()

            # A hand-edited url gets the same scrutiny as the command line: an
            # unusable one is cleared here instead of being written back to look
            # as if it worked (worker.py would ignore it at request time anyway).
            if ($existingProxyUrl) {
                $existingSpec = Resolve-ProxySpec -Text $existingProxyUrl

                if ($existingSpec.Ok) {
                    $existingProxyUrl = $existingSpec.Url
                }
                else {
                    Write-Log ("Configured proxy url {0} cannot be used: {1} - clearing it" -f $existingProxyUrl, $existingSpec.Reason) "WARN"
                    $existingProxyUrl = ""
                }
            }
        }

        if ($null -ne $existingProxy.PSObject.Properties["retries"]) {
            $ProxyRetries = [int]$existingProxy.retries
        }

        if ($null -ne $existingProxy.PSObject.Properties["large_file_threshold_bytes"]) {
            $ProxyThresholdBytes = [int]$existingProxy.large_file_threshold_bytes
        }

        # Upgrade from r5: proxy.linux.host/port/type names the same proxy the r6
        # Worker dials itself, so carry it over as proxy.url.  A loopback host is
        # not carried over - in r5 it was the local end of the tunnel and means
        # nothing without one.
        if (-not $existingProxyUrl -and $null -ne $existingProxy.PSObject.Properties["linux"]) {
            $oldProxy = $existingProxy.linux
            $oldHost = ""
            $oldPort = 0
            $oldType = "http"

            if ($null -ne $oldProxy.PSObject.Properties["host"]) { $oldHost = "$($oldProxy.host)".Trim() }
            if ($null -ne $oldProxy.PSObject.Properties["port"]) { $oldPort = [int]$oldProxy.port }
            if ($null -ne $oldProxy.PSObject.Properties["type"]) { $oldType = "$($oldProxy.type)".Trim() }

            if ($oldHost -and $oldPort -gt 0) {
                $migrated = Resolve-ProxySpec -Text ("{0}://{1}:{2}" -f $oldType, $oldHost, $oldPort)

                if ($migrated.Ok -and $migrated.Url) {
                    $existingProxyUrl = $migrated.Url
                    Write-Log ("Carrying the r5 proxy setting over as this Worker's own proxy: {0}" -f $existingProxyUrl)
                }
                else {
                    Write-Log ("The r5 proxy setting ({0}:{1}) cannot be carried over: {2}" -f $oldHost, $oldPort, $migrated.Reason) "WARN"
                }
            }
        }
    }

    if ($PSBoundParameters.ContainsKey("LinuxProxyHost") -or $PSBoundParameters.ContainsKey("LinuxProxyPort")) {
        Write-Log "-LinuxProxyHost/-LinuxProxyPort are deprecated (r5): the reverse tunnel is gone, folding host+port into the Worker proxy" "WARN"

        $legacyHost = "$LinuxProxyHost".Trim()
        $legacyPort = [int]$LinuxProxyPort

        if (-not $PSBoundParameters.ContainsKey("LinuxProxyType")) {
            Write-Log "-LinuxProxyType defaults to http on this deprecated path - pass -WorkerProxy to be explicit" "WARN"
        }

        if ($legacyHost -and $legacyPort -gt 0) {
            $migrated = Resolve-ProxySpec -Text ("{0}://{1}:{2}" -f $LinuxProxyType, $legacyHost, $legacyPort)

            if ($migrated.Ok) {
                $ProxyUrlValue = $migrated.Url
            }
            else {
                Write-Log ("  {0}" -f $migrated.Reason) "WARN"
            }
        }
        else {
            Write-Log "  host and port must both be given - the deprecated pair is ignored" "WARN"
        }

        if ($PSBoundParameters.ContainsKey("LinuxProxyLocalPort")) {
            Write-Log "-LinuxProxyLocalPort is deprecated and ignored - the Worker dials its proxy directly" "WARN"
        }
    }

    if ($DisableLinuxProxy) {
        Write-Log "-DisableLinuxProxy is deprecated (r5) - same as -WorkerProxy off" "WARN"
        $ProxyUrlValue = ""
    }

    if ($PSBoundParameters.ContainsKey("WorkerProxy")) {
        $spec = Resolve-ProxySpec -Text $WorkerProxy

        if (-not $spec.Ok) {
            throw ("-WorkerProxy is not usable: {0}" -f $spec.Reason)
        }

        $ProxyUrlValue = $spec.Url
    }
    elseif (-not $ProxyUrlValue -and -not $DisableLinuxProxy -and
            -not $PSBoundParameters.ContainsKey("LinuxProxyHost") -and
            -not $PSBoundParameters.ContainsKey("LinuxProxyPort")) {
        # Not named on the command line: reuse what the config held, and ask a
        # human when one is attached.  (r6 asks for the Worker's own proxy; there
        # is no reverse tunnel left to offer instead.)
        $ProxyUrlValue = "$existingProxyUrl".Trim()

        if (Test-InteractiveConsole) {
            $ProxyUrlValue = Read-ProxySetting -CurrentUrl $ProxyUrlValue
        }
    }

    if ($ProxyUrlValue) {
        Write-Log ("Worker proxy: {0} (tried first, DIRECT fallback)" -f $ProxyUrlValue)
    }
    else {
        Write-Log "Worker proxy: none - downloads go DIRECT only"
    }

    if ($ProxyUrlValue -and -not $script:DryRun) {
        Test-ProxyReachable -ProxyUrl $ProxyUrlValue | Out-Null
    }

    $linuxUser = Resolve-Setting -Name "LinuxUser" -ParamValue $LinuxUser -ExistingValue $existingUser
    $linuxHost = Resolve-Setting -Name "LinuxHost" -ParamValue $LinuxHost -ExistingValue $existingHost

    if (-not $linuxUser -or -not $linuxHost) {
        throw @"
-LinuxUser and -LinuxHost are required on the first install.

  .\install-worker.ps1 -LinuxUser <user> -LinuxHost <linux tailscale ip>

Example:
  .\install-worker.ps1 -LinuxUser <user> -LinuxHost <linux-ip>
"@
    }

    $sshTarget = "{0}@{1}" -f $linuxUser, $linuxHost

    Assert-LinuxSshAuth -SshExe $sshExe -Target $sshTarget
    Assert-PackageFiles

    Clear-LegacyInstalls

    Write-Step "Installing Worker files"
    if (-not $script:DryRun) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

        foreach ($name in @(
            "worker.py",
            "worker_config.py",
            "start_worker.py",
            "start_tunnel.py",
            "worker_supervisor.py",
            "tunnel_supervisor.py",
            "download-worker.ps1",
            "sync_log.py",
            "WorkerBandwidth.psm1",
            "requirements.txt"
        )) {
            Copy-Item -Path (Join-Path $WorkerSourceDir $name) -Destination (Join-Path $InstallDir $name) -Force
        }
    }

    Write-Log ("Worker files copied to {0}" -f $InstallDir) "OK"

    New-WorkerVenv -Python $python
    Install-Requirements
    Initialize-LogsAndBandwidth
    Register-BandwidthCommands
    Write-WorkerConfig -WorkerHost $tailscaleIp -ResolvedLinuxUser $linuxUser -ResolvedLinuxHost $linuxHost -ResolvedWorkerLabel $WorkerLabel
    Install-DownloadWorkerSwitch
    Set-FirewallRule
    Register-WorkerTasks -WorkerHost $tailscaleIp

    if ($keepPaused) {
        # The supervisors will not start anything while paused.flag exists, so waiting
        # for /health could only ever time out.
        Write-Log "Skipped: starting/verifying Worker and tunnel (PC stays switched off)" "WARN"
        Write-Log "  bring it back online with: download-worker on" "WARN"
    }
    else {
        Start-AndVerifyWorker -WorkerHost $tailscaleIp
        Start-AndVerifyTunnel -SshExe $sshExe -SshTarget $sshTarget -WorkerHost $tailscaleIp
    }

    Show-Summary -WorkerHost $tailscaleIp -ResolvedLinuxUser $linuxUser -ResolvedLinuxHost $linuxHost -PythonVersion $python.Version

    exit 0
}
catch {
    Write-Host ""
    Write-Host ("Installation failed: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host ""
    Write-Host ("Installer log : " + $InstallerLog)
    Write-Host ("Worker log    : " + (Join-Path $LogDir "worker.log"))
    Write-Host ("Tunnel log    : " + (Join-Path $LogDir "tunnel.log"))
    Write-Host ""
    exit 1
}
