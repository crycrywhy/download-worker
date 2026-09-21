<#
.SYNOPSIS
    Verify a Local Download Worker installation on this Windows PC.

.DESCRIPTION
    Read-only acceptance check (nothing is changed):
      files, virtual environment, dependencies, configuration, scheduled tasks,
      firewall rule, Worker /health, tunnel process, Linux side reachability.

    Exit code 0 = all checks passed, 1 = at least one check failed.

.PARAMETER InstallDir
    Installation directory.  Default: C:\ProgramData\LocalDownloadWorker

.EXAMPLE
    .\verify-install.ps1
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$InstallDir = "C:\ProgramData\LocalDownloadWorker"
)

$ErrorActionPreference = "Continue"

$script:WorkerTaskName = "Local Download Worker"
$script:TunnelTaskName = "Local Download Tunnel"
$script:LogSyncTaskName = "Local Download Log Sync"
$script:Failures = 0

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$ConfigPath = Join-Path $InstallDir "worker-config.json"
$VenvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"

function Add-Result {
    param(
        [string]$Name,
        [bool]$Passed,
        [string]$Detail = ""
    )

    if ($Passed) {
        Write-Host ("[PASS] {0,-34} {1}" -f $Name, $Detail) -ForegroundColor Green
    }
    else {
        Write-Host ("[FAIL] {0,-34} {1}" -f $Name, $Detail) -ForegroundColor Red
        $script:Failures++
    }
}

function Add-Skip {
    param(
        [string]$Name,
        [string]$Detail = ""
    )

    Write-Host ("[SKIP] {0,-34} {1}" -f $Name, $Detail) -ForegroundColor Yellow
}

function Test-HealthUrl {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 5
    )

    try {
        $request = [System.Net.HttpWebRequest]::Create($Url)
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
        return $null
    }
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Host ""
Write-Host ("=" * 56)
Write-Host "Local Download Worker - installation check"
Write-Host ("=" * 56)
Write-Host ""

$isAdmin = Test-Administrator

# --- configuration -----------------------------------------------------------
$config = $null

if (Test-Path $ConfigPath) {
    try {
        $config = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Add-Result "worker-config.json" $true $ConfigPath
    }
    catch {
        Add-Result "worker-config.json" $false ("unreadable: " + $_.Exception.Message)
    }
}
else {
    Add-Result "worker-config.json" $false ("missing: " + $ConfigPath)
}

$workerHost = $null
$workerPort = 8765
$linuxUser = $null
$linuxHost = $null
$tunnelPort = 8766
$remoteDir = $null
$logSyncEnabled = $false

if ($null -ne $config) {
    $workerHost = "$($config.worker_host)".Trim()
    $workerPort = [int]$config.worker_port
    $linuxUser = "$($config.linux_user)".Trim()
    $linuxHost = "$($config.linux_host)".Trim()
    $tunnelPort = [int]$config.linux_tunnel_port
    $remoteDir = "$($config.log_sync.remote_dir)".Trim()
    $logSyncEnabled = [bool]$config.log_sync.enabled

    if (-not $workerHost) {
        $exe = "C:\Program Files\Tailscale\tailscale.exe"
        if (Test-Path $exe) {
            $workerHost = (@(& $exe ip -4 2>$null) | Select-Object -First 1)
            $workerHost = "$workerHost".Trim()
        }
    }
}

# --- files -------------------------------------------------------------------
$files = @(
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

if (Test-Path $InstallDir) {
    Add-Result "install directory" $true $InstallDir
}
else {
    Add-Result "install directory" $false ("missing: " + $InstallDir)
}

$missing = @()
foreach ($name in $files) {
    if (-not (Test-Path (Join-Path $InstallDir $name))) {
        $missing += $name
    }
}

Add-Result "worker files" ($missing.Count -eq 0) ("missing: " + ($missing -join ", "))

# --- python / venv -----------------------------------------------------------
if (Test-Path $VenvPython) {
    $version = (& $VenvPython -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null)
    Add-Result "virtual environment" ($LASTEXITCODE -eq 0) ("Python " + "$version".Trim())

    $deps = & $VenvPython -m pip list --disable-pip-version-check --format=freeze 2>$null
    foreach ($package in @("fastapi", "httpx", "uvicorn")) {
        $found = @($deps | Where-Object { $_ -like "$package==*" })
        Add-Result ("dependency: " + $package) ($found.Count -gt 0) ($found -join ",")
    }
}
else {
    Add-Result "virtual environment" $false ("missing: " + $VenvPython)
}

# --- logs / bandwidth --------------------------------------------------------
$logDir = Join-Path $InstallDir "logs"
Add-Result "logs directory" (Test-Path $logDir) $logDir

$bandwidthFile = Join-Path $logDir "bandwidth.json"
if (Test-Path $bandwidthFile) {
    try {
        $percent = [int]((Get-Content $bandwidthFile -Raw -Encoding UTF8 | ConvertFrom-Json).percent)
        Add-Result "bandwidth.json" $true ("{0}% / {1} MB/s" -f $percent, ($percent / 10))
    }
    catch {
        Add-Result "bandwidth.json" $false "unreadable"
    }
}
else {
    Add-Result "bandwidth.json" $false "missing (Worker will use 100%)"
}

# --- scheduled tasks ---------------------------------------------------------
if ($isAdmin) {
    foreach ($taskName in @($script:WorkerTaskName, $script:TunnelTaskName, $script:LogSyncTaskName)) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

        if ($null -eq $task) {
            Add-Result ("task: " + $taskName) $false "not found"
        }
        else {
            # LogonType is part of the result on purpose: S4U = session 0 (survives every
            # logon/logoff); Interactive = the task lives inside a logon session, so an SSH
            # or RDP session that started it will take it down on exit.
            Add-Result ("task: " + $taskName) ($task.State -ne "Disabled") `
                ("state: {0} | LogonType: {1}" -f $task.State, $task.Principal.LogonType)
        }
    }

    # --- firewall ------------------------------------------------------------
    $rules = @(Get-NetFirewallRule -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like "Local Download Worker*" })

    if ($rules.Count -eq 0) {
        Add-Result "firewall rule" $false "not found"
    }
    else {
        foreach ($rule in $rules) {
            $portFilter = $rule | Get-NetFirewallPortFilter
            $addressFilter = $rule | Get-NetFirewallAddressFilter
            $detail = "{0} | TCP {1} | remote {2} | enabled={3}" -f `
                $rule.DisplayName, $portFilter.LocalPort, $addressFilter.RemoteAddress, $rule.Enabled
            Add-Result "firewall rule" ($rule.Enabled -eq "True" -and "$($portFilter.LocalPort)" -eq "$workerPort") $detail
        }
    }
}
else {
    Add-Skip "scheduled tasks" "needs an elevated PowerShell window"
    Add-Skip "firewall rule" "needs an elevated PowerShell window"
}

# --- registered commands -----------------------------------------------------
$powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$probe = ""

# run the command the way a freshly opened window would: no inherited per-process
# execution policy, and a real load of the module instead of a name lookup
$inheritedPolicy = [Environment]::GetEnvironmentVariable("PSExecutionPolicyPreference")
Remove-Item Env:\PSExecutionPolicyPreference -ErrorAction SilentlyContinue

try {
    $probe = & $powerShellExe -NoProfile -Command "Import-Module LocalDownloadWorker -Force -ErrorAction Continue; Get-WorkerBandwidth" 2>&1
}
catch {
    $probe = "$_"
}
finally {
    if ($inheritedPolicy) { $env:PSExecutionPolicyPreference = $inheritedPolicy }
}

$probeText = "$probe".Trim()

if ($probeText -match "Worker bandwidth:") {
    Add-Result "Get-WorkerBandwidth" $true $probeText
}
else {
    $policy = ""
    try { $policy = & $powerShellExe -NoProfile -Command "Get-ExecutionPolicy" 2>&1 } catch { $policy = "$_" }
    Add-Result "Get-WorkerBandwidth" $false ("{0} | execution policy: {1}" -f $probeText, "$policy".Trim())
}

# --- switch command + supervision ---------------------------------------
$binDir = Join-Path $env:USERPROFILE "bin"
$shimPath = Join-Path $binDir "download-worker.cmd"
Add-Result "download-worker switch" (Test-Path $shimPath) $shimPath

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
$binOnPath = @(($userPath -split ";") | Where-Object { $_ -and $_.TrimEnd("\") -ieq $binDir.TrimEnd("\") }).Count -gt 0
Add-Result "user PATH has bin dir" $binOnPath $binDir

if ($isAdmin) {
    $tunnelTask = Get-ScheduledTask -TaskName $script:TunnelTaskName -ErrorAction SilentlyContinue
    $tunnelAction = ""
    if ($null -ne $tunnelTask) { $tunnelAction = "$($tunnelTask.Actions[0].Arguments)".Trim() }
    $supervised = $tunnelAction -like "*tunnel_supervisor.py*"
    Add-Result "tunnel task is supervised" $supervised $tunnelAction.Trim('"')
}

$tunnelSup = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*tunnel_supervisor.py*" })
Add-Result "tunnel supervisor running" ($tunnelSup.Count -gt 0) ("{0} process(es)" -f $tunnelSup.Count)

$pauseFlag = Join-Path $InstallDir "paused.flag"
if (Test-Path $pauseFlag) {
    Write-Host "[NOTE] paused.flag exists - this PC is switched OFF on purpose (download-worker on)" -ForegroundColor Yellow
}

# --- worker health -----------------------------------------------------------
if ($workerHost) {
    $body = Test-HealthUrl -Url ("http://{0}:{1}/health" -f $workerHost, $workerPort)
    Add-Result "Worker /health (tailscale ip)" ($null -ne $body -and $body -match '"status"\s*:\s*"ok"') "$body".Trim()
}
else {
    Add-Result "Worker /health (tailscale ip)" $false "no worker_host in the configuration"
}

$localBody = Test-HealthUrl -Url ("http://127.0.0.1:{0}/health" -f $workerPort) -TimeoutSeconds 3
Add-Result "Worker /health (localhost)" ($null -ne $localBody) "$localBody".Trim()

# --- tunnel ------------------------------------------------------------------
$ssh = Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*-R ${tunnelPort}:*" }

Add-Result "tunnel process" ($null -ne $ssh) ("ssh -R ${tunnelPort}:...")

if ($linuxHost -and $linuxUser) {
    $remote = ""
    try {
        $remote = & ssh.exe -o BatchMode=yes -o ConnectTimeout=15 ("{0}@{1}" -f $linuxUser, $linuxHost) `
            ("curl -fsS -m 5 http://127.0.0.1:{0}/health" -f $tunnelPort) 2>&1
    }
    catch {
        $remote = "$_"
    }

    Add-Result "Linux -> Worker via tunnel" ("$remote" -match '"status"\s*:\s*"ok"') "$remote".Trim()

    if ($remoteDir) {
        $dirCheck = ""
        try {
            $dirCheck = & ssh.exe -o BatchMode=yes -o ConnectTimeout=15 ("{0}@{1}" -f $linuxUser, $linuxHost) `
                ("test -d '{0}' && echo DIR_OK" -f $remoteDir) 2>&1
        }
        catch {
            $dirCheck = "$_"
        }

        Add-Result "Linux log directory" ("$dirCheck" -match "DIR_OK") $remoteDir
    }
}
else {
    Add-Result "Linux -> Worker via tunnel" $false "no linux_user / linux_host in the configuration"
}

# --- summary -----------------------------------------------------------------
Write-Host ""
if ($script:Failures -eq 0) {
    Write-Host "RESULT: ALL CHECKS PASSED" -ForegroundColor Green
    exit 0
}

Write-Host ("RESULT: {0} CHECK(S) FAILED" -f $script:Failures) -ForegroundColor Red
Write-Host ("Logs: {0}" -f (Join-Path $InstallDir "logs"))
exit 1
