# run_all.ps1 — master driver
# Usage:
#   .\run_all.ps1                         # all workloads, all impls
#   .\run_all.ps1 -Workloads 01_vec4_dot_f64,02_mat4_vec4_xform
#   .\run_all.ps1 -Impls danha,cpp_hand_clang
#   .\run_all.ps1 -Repeats 9 -SkipBuild
#   .\run_all.ps1 -Impls danha,cpp_hand_clang -FailOnError
#   .\run_all.ps1 -CompareBaseCsv bench\results\raw_old.csv -FailOnRegression
#
# Outputs:
#   bench/results/raw_<date>.csv
#   bench/results/report_<date>.md
#   bench/results/compare_<base>_to_<date>.md when -CompareBaseCsv is provided
#   bench/build/<workload>_<impl>.exe

param(
    [string[]]$Workloads = @(),       # empty = auto-discover
    [string[]]$Impls = @('danha','cpp_hand_clang','cpp_hand_gpp','cpp_glm','csharp_jit'),
    [int]$Repeats = 7,
    [int]$Warmup = 2,
    [string]$CompareBaseCsv = '',
    [double]$RegressionThreshold = 1.05,
    [double]$ImprovementThreshold = 0.95,
    [switch]$AllowPartialCoverage,
    [switch]$FailOnError,
    [switch]$FailOnRegression,
    [switch]$SkipBuild,
    [switch]$Verbose
)

$ErrorActionPreference = 'Stop'
$benchRoot = Resolve-Path "$PSScriptRoot\.."
$workloadsDir = Join-Path $benchRoot 'workloads'
$buildDir = Join-Path $benchRoot 'build'
$resultsDir = Join-Path $benchRoot 'results'
$thirdParty = Join-Path $benchRoot 'third_party'

. (Join-Path $PSScriptRoot 'measure.ps1')

function Expand-ListParam([string[]]$Items) {
    $out = @()
    foreach ($item in $Items) {
        if ($null -eq $item) { continue }
        foreach ($part in ([string]$item).Split(',')) {
            $trimmed = $part.Trim()
            if ($trimmed) { $out += $trimmed }
        }
    }
    return $out
}

$Workloads = Expand-ListParam $Workloads
$Impls = Expand-ListParam $Impls

# ── Tool resolution ────────────────────────────────────────────────────────
$clangBin = Join-Path $env:LOCALAPPDATA 'LLVM\bin'
if (Test-Path $clangBin) { $env:PATH = "$clangBin;$env:PATH" }
$gppPath = 'C:\Users\이현욱\AppData\Local\Microsoft\WinGet\Packages\BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe\mingw64\bin\g++.exe'
$dotnetPath = 'C:\Program Files\dotnet\dotnet.exe'
$danhaPy = Join-Path (Split-Path $benchRoot) 'danha.py'
if (-not (Test-Path $danhaPy)) { $danhaPy = 'C:\Users\이현욱\Desktop\Danha\danha.py' }

# ── Workload discovery ─────────────────────────────────────────────────────
if ($Workloads.Count -eq 0) {
    $Workloads = Get-ChildItem $workloadsDir -Directory | Sort-Object Name | ForEach-Object { $_.Name }
}

if ($Workloads.Count -eq 0) {
    Write-Host "[no workloads found in $workloadsDir]" -ForegroundColor Yellow
    exit 0
}

Write-Host "=== Workloads: $($Workloads -join ', ') ===" -ForegroundColor Cyan
Write-Host "=== Impls: $($Impls -join ', ') ==="           -ForegroundColor Cyan

# ── Build functions ────────────────────────────────────────────────────────
function Build-Danha($wl, $exeOut) {
    $src = Join-Path $workloadsDir "$wl\danha\bench.dh"
    if (-not (Test-Path $src)) { return $false }
    # danha compile outputs `<srcdir>/<basename>.exe`
    & python $danhaPy compile $src 2>&1 | Out-Null
    $defaultExe = Join-Path $workloadsDir "$wl\danha\bench.exe"
    if (Test-Path $defaultExe) {
        Move-Item $defaultExe $exeOut -Force
        return $true
    }
    return $false
}

function Build-CppHandClang($wl, $exeOut) {
    $src = Join-Path $workloadsDir "$wl\cpp_hand\bench.cpp"
    if (-not (Test-Path $src)) { return $false }
    & clang++ -O3 -march=native -std=c++20 $src -o $exeOut 2>&1 | Out-Null
    return (Test-Path $exeOut)
}

function Build-CppHandGpp($wl, $exeOut) {
    $src = Join-Path $workloadsDir "$wl\cpp_hand\bench.cpp"
    if (-not (Test-Path $src)) { return $false }
    & $gppPath -O3 -march=native -std=c++20 $src -o $exeOut 2>&1 | Out-Null
    return (Test-Path $exeOut)
}

function Build-CppGlm($wl, $exeOut) {
    $src = Join-Path $workloadsDir "$wl\cpp_glm\bench.cpp"
    if (-not (Test-Path $src)) { return $false }
    $glmInc = Join-Path $thirdParty 'glm'
    if (-not (Test-Path (Join-Path $glmInc 'glm\glm.hpp'))) {
        # glm not installed — skip
        return $false
    }
    & clang++ -O3 -march=native -std=c++20 "-I$glmInc" $src -o $exeOut 2>&1 | Out-Null
    return (Test-Path $exeOut)
}

function Build-CsharpAot($wl, $exeOut) {
    $proj = Join-Path $workloadsDir "$wl\csharp\Bench.csproj"
    if (-not (Test-Path $proj)) { return $false }
    $pubOut = Join-Path $buildDir "${wl}_csharp_aot"
    & $dotnetPath publish $proj -c Release -r win-x64 -o $pubOut 2>&1 | Out-Null
    $pubExe = Join-Path $pubOut 'Bench.exe'
    if (Test-Path $pubExe) {
        Copy-Item $pubExe $exeOut -Force
        return $true
    }
    return $false
}

function Build-CsharpJit($wl, $exeOut) {
    $proj = Join-Path $workloadsDir "$wl\csharp\Bench.csproj"
    if (-not (Test-Path $proj)) { return $false }
    $buildOut = Join-Path $buildDir "${wl}_csharp_jit"
    & $dotnetPath build $proj -c Release -o $buildOut /p:PublishAot=false /p:UseAppHost=true 2>&1 | Out-Null
    # Apphost needs neighbor Bench.dll + runtimeconfig.json — leave it in the build subdir.
    # Write a pointer file '<exeOut>.target' containing the apphost path; measure picks it up.
    $appHost = Join-Path $buildOut 'Bench.exe'
    if (Test-Path $appHost) {
        $target = "$exeOut.target"
        # Use UTF-8 BOM so PowerShell reads it back correctly on Korean systems
        [System.IO.File]::WriteAllText($target, $appHost, [System.Text.UTF8Encoding]::new($true))
        return $true
    }
    return $false
}

$builders = @{
    'danha'           = ${function:Build-Danha}
    'cpp_hand_clang'  = ${function:Build-CppHandClang}
    'cpp_hand_gpp'    = ${function:Build-CppHandGpp}
    'cpp_glm'         = ${function:Build-CppGlm}
    'csharp_aot'      = ${function:Build-CsharpAot}
    'csharp_jit'      = ${function:Build-CsharpJit}
}

# ── Build all ──────────────────────────────────────────────────────────────
$buildResults = @{}
if (-not $SkipBuild) {
    foreach ($wl in $Workloads) {
        foreach ($impl in $Impls) {
            $exe = Join-Path $buildDir "${wl}_${impl}.exe"
            if (Test-Path $exe) { Remove-Item $exe -Force }
            Write-Host "  build $wl / $impl ... " -NoNewline
            $ok = & $builders[$impl] $wl $exe
            $buildResults["$wl|$impl"] = $ok
            if ($ok) { Write-Host 'ok' -ForegroundColor Green }
            else     { Write-Host 'skip' -ForegroundColor DarkGray }
        }
    }
}

# ── Equivalence check ──────────────────────────────────────────────────────
function Get-LastNumericLine($path) {
    if (-not (Test-Path $path)) { return $null }
    (Get-Content $path | Where-Object { $_ -ne "" })[-1]
}

# ── Run ────────────────────────────────────────────────────────────────────
$timestamp = "$(Get-Date -Format 'yyyy-MM-dd_HH-mm-ss-fff')_$PID"
$csvPath = Join-Path $resultsDir "raw_$timestamp.csv"
$mdPath  = Join-Path $resultsDir "report_$timestamp.md"

$rows = @()

foreach ($wl in $Workloads) {
    Write-Host "`n=== $wl ===" -ForegroundColor Cyan
    $expectedPath = Join-Path $workloadsDir "$wl\expected.txt"
    $expected = $null
    if (Test-Path $expectedPath) {
        $expected = (Get-Content $expectedPath -Raw).Trim()
    }
    $argsPath = Join-Path $workloadsDir "$wl\args.txt"
    $cliArgs = @()
    if (Test-Path $argsPath) {
        $cliArgs = (Get-Content $argsPath -Raw).Trim().Split(' ') | Where-Object { $_ }
    }

    foreach ($impl in $Impls) {
        $exe = Join-Path $buildDir "${wl}_${impl}.exe"
        $exeCmd = "${exe}.cmd"
        $exeTarget = "${exe}.target"
        $launch = $exe
        if (Test-Path $exeTarget) {
            $launch = (Get-Content $exeTarget -Raw -Encoding UTF8).Trim()
        } elseif (Test-Path $exeCmd) {
            $launch = $exeCmd
        }
        if (-not (Test-Path $launch)) {
            $rows += [pscustomobject]@{
                workload=$wl; impl=$impl; build_ok=$false; equiv_ok=$false
                min_ns=$null; median_ns=$null; wall_min_ns=$null; wall_median_ns=$null
                output_last=""; ratio_vs_danha=$null
            }
            continue
        }

        $r = Invoke-BenchRun -ExePath $launch -ExeArgs $cliArgs -Warmup $Warmup -Repeats $Repeats
        $equivOk = $true
        if ($expected) {
            $equivOk = ([string]$r.OutputLast).Trim() -eq $expected
        }

        # Prefer internal ELAPSED_NS, fall back to wall clock
        $effMin    = if ($r.MinNs)    { $r.MinNs }    else { $r.WallMinNs }
        $effMedian = if ($r.MedianNs) { $r.MedianNs } else { $r.WallMedianNs }
        $msStr = Format-Ms $effMin
        $mark  = if ($equivOk) { '✓' } else { '✗' }
        Write-Host ("  {0,-18}  {1}  {2}  out={3}" -f $impl, $msStr, $mark, $r.OutputLast)

        $rows += [pscustomobject]@{
            workload=$wl; impl=$impl
            build_ok=$true; equiv_ok=$equivOk
            min_ns=$effMin; median_ns=$effMedian
            wall_min_ns=$r.WallMinNs; wall_median_ns=$r.WallMedianNs
            output_last=$r.OutputLast
            ratio_vs_danha=$null  # filled below
        }
    }
}

# ── Compute ratios ─────────────────────────────────────────────────────────
$danhaTimes = @{}
foreach ($r in $rows) {
    if ($r.impl -eq 'danha' -and $r.min_ns) { $danhaTimes[$r.workload] = $r.min_ns }
}
foreach ($r in $rows) {
    if ($r.min_ns -and $danhaTimes.ContainsKey($r.workload) -and $danhaTimes[$r.workload]) {
        $r.ratio_vs_danha = [Math]::Round($r.min_ns / $danhaTimes[$r.workload], 3)
    }
}

# ── Write CSV ──────────────────────────────────────────────────────────────
$rows | Export-Csv -Path $csvPath -NoTypeInformation -Encoding UTF8
Write-Host "`nCSV → $csvPath" -ForegroundColor Green

# ── Write markdown report ──────────────────────────────────────────────────
. (Join-Path $PSScriptRoot 'report.ps1')
Write-MarkdownReport -CsvPath $csvPath -MdPath $mdPath -Impls $Impls
Write-Host "Report → $mdPath" -ForegroundColor Green

$buildFailures = @($rows | Where-Object { $_.build_ok -ne $true -and $_.build_ok -ne 'True' }).Count
$equivFailures = @($rows | Where-Object { $_.equiv_ok -ne $true -and $_.equiv_ok -ne 'True' }).Count
$missingTiming = @($rows | Where-Object { -not $_.min_ns }).Count
Write-Host ("Run issues: build={0}, equiv={1}, missing_timing={2}" -f $buildFailures, $equivFailures, $missingTiming)

if ($CompareBaseCsv) {
    $basePath = Resolve-Path $CompareBaseCsv
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($basePath.Path)
    $comparePath = Join-Path $resultsDir "compare_${baseName}_to_$timestamp.md"
    $gateFailOnRegression = [bool]$FailOnRegression
    $gateAllowPartialCoverage = [bool]$AllowPartialCoverage
    $gateRegressionThreshold = $RegressionThreshold
    $gateImprovementThreshold = $ImprovementThreshold
    . (Join-Path $PSScriptRoot 'compare.ps1')
    $summary = Write-BenchmarkComparison `
        -BaseCsv $basePath.Path `
        -CurrentCsv $csvPath `
        -MdPath $comparePath `
        -RegressionThreshold $gateRegressionThreshold `
        -ImprovementThreshold $gateImprovementThreshold `
        -AllowPartialCoverage:$gateAllowPartialCoverage
    Write-Host "Compare → $comparePath" -ForegroundColor Green
    Write-Host ("Comparable rows: {0}, coverage issues: {1}, regressions: {2}, build/equiv issues: {3}" -f $summary.ComparableRows, $summary.CoverageIssueCount, $summary.RegressionCount, $summary.BuildEquivIssueCount)
    if ($gateFailOnRegression -and (
        $summary.RegressionCount -gt 0 -or
        $summary.BuildEquivIssueCount -gt 0 -or
        ((-not $gateAllowPartialCoverage) -and $summary.CoverageIssueCount -gt 0)
    )) {
        Write-Host "Benchmark delta gate failed." -ForegroundColor Red
        exit 1
    }
}

if ($FailOnError -and ($buildFailures -gt 0 -or $equivFailures -gt 0 -or $missingTiming -gt 0)) {
    Write-Host "Benchmark run gate failed." -ForegroundColor Red
    exit 1
}
