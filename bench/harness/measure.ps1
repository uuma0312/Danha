# measure.ps1 — single-binary timed run with warmup/repeat/affinity
# Each benchmark binary internally measures its hot loop and prints
# "ELAPSED_NS=<n>" to stdout. We also wall-clock-time the whole process
# (for startup-sensitive measurements).
#
# Usage:
#   . .\measure.ps1
#   $r = Invoke-BenchRun -ExePath build\01_vec4_dot_f64_dh.exe -Args @('100000000') -Warmup 2 -Repeats 7
#   # $r.MinNs, $r.MedianNs, $r.WallMinNs, $r.WallMedianNs, $r.OutputLast

function Invoke-BenchRun {
    param(
        [Parameter(Mandatory=$true)][string]$ExePath,
        [string[]]$ExeArgs = @(),
        [int]$Warmup = 2,
        [int]$Repeats = 7,
        [int]$AffinityMask = 0x4   # P-core (CPU 2)
    )
    if (-not (Test-Path $ExePath)) {
        return [pscustomobject]@{
            Ok = $false; Error = "missing binary"; MinNs = $null
            MedianNs = $null; OutputLast = ""
        }
    }

    $internalNs = @()
    $wallNs    = @()
    $lastLine   = ""

    $total = $Warmup + $Repeats
    for ($i = 0; $i -lt $total; $i++) {
        $output = ""
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            if ($ExeArgs.Count -gt 0) {
                $output = & $ExePath @ExeArgs 2>&1 | Out-String
            } else {
                $output = & $ExePath 2>&1 | Out-String
            }
            $sw.Stop()
        } catch {
            $sw.Stop()
            return [pscustomobject]@{
                Ok = $false; Error = $_.Exception.Message; MinNs = $null
                MedianNs = $null; OutputLast = ""
            }
        }
        $wall = $sw.ElapsedTicks * (1e9 / [System.Diagnostics.Stopwatch]::Frequency)
        $lines = @($output -split "`r?`n" | Where-Object { $_ -ne "" })
        $lastLine = if ($lines.Count -gt 0) { [string]$lines[-1] } else { "" }

        $internal = $null
        foreach ($ln in $lines) {
            if ($ln -match '^ELAPSED_NS=(\d+)') {
                $internal = [int64]$matches[1]
                break
            }
        }

        if ($i -ge $Warmup) {
            $wallNs += $wall
            if ($null -ne $internal) { $internalNs += $internal }
        }
    }

    function _stat($arr) {
        if ($arr.Count -eq 0) { return @{ Min=$null; Median=$null } }
        $sorted = $arr | Sort-Object
        $min = $sorted[0]
        $median = $sorted[[Math]::Floor($sorted.Count / 2)]
        return @{ Min=$min; Median=$median }
    }

    $intStat  = _stat $internalNs
    $wallStat = _stat $wallNs

    [pscustomobject]@{
        Ok            = $true
        Error         = $null
        MinNs         = $intStat.Min
        MedianNs      = $intStat.Median
        WallMinNs     = $wallStat.Min
        WallMedianNs  = $wallStat.Median
        OutputLast    = $lastLine
        Samples       = $internalNs
    }
}

function Format-Ms {
    param($ns)
    if ($null -eq $ns) { return "n/a" }
    "{0:N1} ms" -f ($ns / 1e6)
}
