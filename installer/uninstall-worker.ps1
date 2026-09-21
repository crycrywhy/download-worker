<#
.SYNOPSIS
    Remove the Local Download Worker from this Windows PC.

.DESCRIPTION
    Removes the scheduled tasks (Worker / Tunnel / Log Sync), the firewall rule
    and the installation directory.

    It does NOT touch anything that belongs to the system rather than to the
    Worker: Tailscale, SSH keys, Windows OpenSSH, Linux sshd configuration.

.PARAMETER InstallDir
    Installation directory.  Default: C:\ProgramData\LocalDownloadWorker

.PARAMETER KeepFiles
    Remove tasks and firewall rule, but keep the installation directory.

.PARAMETER LinuxTunnelPort
    Tunnel port used by this PC (only needed to stop a leftover ssh process).
    Default 8766.

.PARAMETER Force
    Do not ask for confirmation.

.EXAMPLE
    .\uninstall-worker.ps1

.EXAMPLE
    .\uninstall-worker.ps1 -InstallDir "D:\LocalDownloadWorker" -KeepFiles
#>

#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$InstallDir = "C:\ProgramData\LocalDownloadWorker",

    [switch]$KeepFiles,

    [int]$LinuxTunnelPort = 8766,

    [switch]$Force
)

$ErrorActionPreference = "Stop"

$script:WorkerTaskName = "Local Download Worker"
$script:TunnelTaskName = "Local Download Tunnel"
$script:LogSyncTaskName = "Local Download Log Sync"

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)


function Write-Log {
    param(
        [string]$Message,
        [ValidateSet("INFO", "OK", "WARN", "ERROR")][string]$Level = "INFO"
    )

    switch ($Level) {
        "OK"    { Write-Host $Message -ForegroundColor Green }
        "WARN"  { Write-Host $Message -ForegroundColor Yellow }
        "ERROR" { Write-Host $Message -ForegroundColor Red }
        default { Write-Host $Message }
    }
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)

    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This uninstaller must run in an elevated PowerShell window (Run as Administrator)."
    }
}

function Stop-WorkerProcesses {
    $stopped = 0

    # Match by executable location as well as by command line: a venv python.exe is
    # "inside" the install directory even when its command line was rewritten.
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.ExecutablePath -and $_.ExecutablePath -like "$InstallDir\*") -or
            ($_.CommandLine -and $_.CommandLine -like "*$InstallDir*")
        })

    foreach ($process in $processes) {
        Write-Log ("  stopping {0} (pid {1})" -f $process.Name, $process.ProcessId)
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped++
    }

    # tunnel: ssh.exe -N -R <port>:...
    $sshProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*-R ${LinuxTunnelPort}:*" })

    foreach ($process in $sshProcesses) {
        Write-Log ("  stopping ssh.exe (pid {0})" -f $process.ProcessId)
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped++
    }

    if ($stopped -eq 0) {
        Write-Log "No running Worker / tunnel process found"
    }
}


try {
    Write-Host ""
    Write-Log "Local Download Worker uninstaller"
    Write-Log ("Target directory: {0}" -f $InstallDir)

    Assert-Administrator

    if (-not $Force) {
        Write-Host ""
        Write-Host "This will remove:"
        Write-Host ("  - scheduled tasks: {0}, {1}, {2}" -f $script:WorkerTaskName, $script:TunnelTaskName, $script:LogSyncTaskName)
        Write-Host "  - firewall rule(s) named 'Local Download Worker*'"

        if ($KeepFiles) {
            Write-Host ("  - (files in {0} are kept)" -f $InstallDir)
        }
        else {
            Write-Host ("  - directory: {0}" -f $InstallDir)
        }

        Write-Host ""
        Write-Host "Kept: Tailscale, SSH keys, Windows OpenSSH, Linux sshd configuration."
        Write-Host ""

        $answer = Read-Host "Continue? [y/N]"
        if ($answer -notmatch "^[yY]") {
            Write-Log "Cancelled" "WARN"
            exit 1
        }
    }

    # --- stop running components --------------------------------------------
    Write-Log "Stopping running components"
    Stop-WorkerProcesses

    # --- scheduled tasks ----------------------------------------------------
    foreach ($taskName in @($script:WorkerTaskName, $script:TunnelTaskName, $script:LogSyncTaskName)) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

        if ($null -eq $task) {
            Write-Log ("{0}: not found" -f $taskName)
            continue
        }

        try {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Log ("{0}: removed" -f $taskName) "OK"
        }
        catch {
            Write-Log ("{0}: could not be removed - {1}" -f $taskName, $_.Exception.Message) "WARN"
        }
    }

    # --- firewall -----------------------------------------------------------
    $rules = @(Get-NetFirewallRule -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like "Local Download Worker*" })

    if ($rules.Count -eq 0) {
        Write-Log "Firewall rule: not found"
    }
    else {
        foreach ($rule in $rules) {
            Remove-NetFirewallRule -Name $rule.Name
            Write-Log ("Firewall rule removed: {0}" -f $rule.DisplayName) "OK"
        }
    }

    # --- generated command module (Get-WorkerBandwidth / Set-WorkerBandwidth) --
    $userModulePath = @($env:PSModulePath -split ";" |
        Where-Object { $_ -like "*$env:USERPROFILE*" -and $_ -notlike "*$env:ProgramFiles*" })[0]

    if (-not $userModulePath) {
        $userModulePath = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "WindowsPowerShell\Modules"
    }

    $moduleRoot = Join-Path $userModulePath "LocalDownloadWorker"

    if (Test-Path $moduleRoot) {
        Remove-Item -Path $moduleRoot -Recurse -Force
        Write-Log ("Command module removed: {0}" -f $moduleRoot) "OK"
    }
    else {
        Write-Log "Command module: not found"
    }

    # --- download-worker switch shim ----------------------------------------
    $binDir = Join-Path $env:USERPROFILE "bin"
    $shimPath = Join-Path $binDir "download-worker.cmd"

    if (Test-Path $shimPath) {
        Remove-Item -Path $shimPath -Force
        Write-Log ("Switch command removed: {0}" -f $shimPath) "OK"
    }
    else {
        Write-Log "Switch command: not found"
    }

    # Drop %USERPROFILE%\bin from the user PATH again (only when we added it and it
    # is empty now - another tool may have put its own files there).
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($null -eq $userPath) { $userPath = "" }

    $entries = @($userPath -split ";" | Where-Object { $_ -and $_.Trim() })
    $matches = @($entries | Where-Object { $_.TrimEnd("\") -ieq $binDir.TrimEnd("\") })

    if ($matches.Count -gt 0) {
        $leftover = @(Get-ChildItem -Path $binDir -Force -ErrorAction SilentlyContinue)
        if ($leftover.Count -eq 0) {
            $kept = @($entries | Where-Object { $_.TrimEnd("\") -ine $binDir.TrimEnd("\") })
            [Environment]::SetEnvironmentVariable("Path", ($kept -join ";"), "User")
            Remove-Item -Path $binDir -Force -ErrorAction SilentlyContinue
            Write-Log ("Removed {0} from the user PATH (now empty)" -f $binDir) "OK"
        }
        else {
            Write-Log ("Kept {0} on the user PATH - it still holds {1} other file(s)" -f $binDir, $leftover.Count) "WARN"
        }
    }

    # --- files --------------------------------------------------------------
    if ($KeepFiles) {
        Write-Log ("Files kept: {0}" -f $InstallDir) "WARN"
    }
    elseif (-not (Test-Path $InstallDir)) {
        Write-Log ("Directory not found: {0}" -f $InstallDir)
    }
    else {
        # sanity guard: only delete what really looks like an installation
        $looksLikeInstall =
            (Test-Path (Join-Path $InstallDir "worker.py")) -or
            (Test-Path (Join-Path $InstallDir ".venv")) -or
            (Test-Path (Join-Path $InstallDir "worker-config.json"))

        if (-not $looksLikeInstall -and -not $Force) {
            throw ("{0} does not look like a Local Download Worker installation. Nothing was deleted; re-run with -Force to delete it anyway." -f $InstallDir)
        }

        # A process that exited a second ago may still hold its handles - retry
        # instead of failing on the first "file is in use".
        $removed = $false
        for ($attempt = 1; $attempt -le 5; $attempt++) {
            try {
                Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction Stop
                $removed = $true
                break
            }
            catch {
                if ($attempt -lt 5) { Start-Sleep -Seconds 2 }
                else {
                    Write-Log ("Could not remove {0}: {1}" -f $InstallDir, $_.Exception.Message) "WARN"
                    foreach ($item in @(Get-ChildItem -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue |
                                        Select-Object -First 5)) {
                        Write-Log ("  still present: {0}" -f $item.FullName) "WARN"
                    }
                    Write-Log "Re-run the uninstaller once those files are free (or reboot)." "WARN"
                }
            }
        }

        if ($removed) { Write-Log ("Directory removed: {0}" -f $InstallDir) "OK" }
    }

    Write-Host ""
    Write-Host ("=" * 56)
    Write-Host "Local Download Worker removed"
    Write-Host ("=" * 56)
    Write-Host ""
    Write-Host "Kept (system environment, not part of the Worker):"
    Write-Host "  Tailscale, SSH keys, Windows OpenSSH, Linux sshd configuration"
    Write-Host ""
    Write-Host "Remember to remove or repoint the Linux side if this PC was in use:"
    Write-Host ("  sshd listener on 127.0.0.1:{0} disappears with the tunnel" -f $LinuxTunnelPort)
    Write-Host ""

    exit 0
}
catch {
    Write-Host ""
    Write-Host ("Uninstall failed: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host ""
    exit 1
}
