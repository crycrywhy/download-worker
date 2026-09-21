function Set-WorkerBandwidth {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateRange(0, 100)]
        [int]$Percent
    )

    $config = Join-Path $PSScriptRoot "logs\bandwidth.json"

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
    $config = Join-Path $PSScriptRoot "logs\bandwidth.json"

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