# compare.ps1 — compare two benchmark CSV files and write a Markdown delta report.

param(
    [string]$BaseCsv,
    [string]$CurrentCsv,
    [string]$MdPath,
    [double]$RegressionThreshold = 1.05,
    [double]$ImprovementThreshold = 0.95,
    [switch]$AllowPartialCoverage,
    [switch]$FailOnRegression
)

function To-DoubleOrNull($value) {
    if ($null -eq $value -or $value -eq '') { return $null }
    return [double]$value
}

function Format-Change($ratio) {
    if ($null -eq $ratio) { return 'n/a' }
    $pct = ($ratio - 1.0) * 100.0
    if ($pct -ge 0) { return '+{0:F1}%' -f $pct }
    return '{0:F1}%' -f $pct
}

function Write-BenchmarkComparison {
    param(
        [Parameter(Mandatory=$true)][string]$BaseCsv,
        [Parameter(Mandatory=$true)][string]$CurrentCsv,
        [Parameter(Mandatory=$true)][string]$MdPath,
        [double]$RegressionThreshold = 1.05,
        [double]$ImprovementThreshold = 0.95,
        [switch]$AllowPartialCoverage
    )

    $baseRows = Import-Csv $BaseCsv
    $curRows = Import-Csv $CurrentCsv

    $baseMap = @{}
    foreach ($row in $baseRows) {
        $baseMap["$($row.workload)|$($row.impl)"] = $row
    }

    $curMap = @{}
    foreach ($row in $curRows) {
        $curMap["$($row.workload)|$($row.impl)"] = $row
    }

    $pairs = @()
    foreach ($cur in $curRows) {
        $key = "$($cur.workload)|$($cur.impl)"
        if (-not $baseMap.ContainsKey($key)) { continue }
        $base = $baseMap[$key]
        $baseNs = To-DoubleOrNull $base.min_ns
        $curNs = To-DoubleOrNull $cur.min_ns
        $ratio = $null
        if ($baseNs -and $curNs) { $ratio = $curNs / $baseNs }
        $pairs += [pscustomobject]@{
            workload = $cur.workload
            impl = $cur.impl
            base_ns = $baseNs
            current_ns = $curNs
            change_ratio = $ratio
            base_equiv = $base.equiv_ok
            current_equiv = $cur.equiv_ok
            base_build = $base.build_ok
            current_build = $cur.build_ok
        }
    }

    $sb = [System.Text.StringBuilder]::new()
    [void]$sb.AppendLine("# Danha Benchmark Comparison")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("Base: ``$BaseCsv``")
    [void]$sb.AppendLine("Current: ``$CurrentCsv``")
    [void]$sb.AppendLine("")

    $baseWorkloads = @($baseRows | Select-Object -ExpandProperty workload -Unique)
    $curWorkloads = @($curRows | Select-Object -ExpandProperty workload -Unique)
    $sharedRows = @($pairs).Count
    $missingCurrentRows = @($baseMap.Keys | Where-Object { -not $curMap.ContainsKey($_) })
    $newCurrentRows = @($curMap.Keys | Where-Object { -not $baseMap.ContainsKey($_) })
    $coverageIssueCount = $missingCurrentRows.Count + $newCurrentRows.Count
    $regressions = @($pairs | Where-Object { $_.change_ratio -and $_.change_ratio -gt $RegressionThreshold })
    $improvements = @($pairs | Where-Object { $_.change_ratio -and $_.change_ratio -lt $ImprovementThreshold })
    $equivIssues = @($pairs | Where-Object {
        $_.base_equiv -ne 'True' -or $_.current_equiv -ne 'True' -or
        $_.base_build -ne 'True' -or $_.current_build -ne 'True'
    })

    [void]$sb.AppendLine("## Coverage")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("| metric | value |")
    [void]$sb.AppendLine("|--------|------:|")
    [void]$sb.AppendLine(("| base workloads | {0} |" -f $baseWorkloads.Count))
    [void]$sb.AppendLine(("| current workloads | {0} |" -f $curWorkloads.Count))
    [void]$sb.AppendLine(("| comparable rows | {0} |" -f $sharedRows))
    [void]$sb.AppendLine(("| missing current rows | {0} |" -f $missingCurrentRows.Count))
    [void]$sb.AppendLine(("| new current rows | {0} |" -f $newCurrentRows.Count))
    [void]$sb.AppendLine(("| coverage issues | {0} |" -f $coverageIssueCount))
    [void]$sb.AppendLine(("| regressions > {0:P0} | {1} |" -f ($RegressionThreshold - 1.0), $regressions.Count))
    [void]$sb.AppendLine(("| improvements > {0:P0} | {1} |" -f (1.0 - $ImprovementThreshold), $improvements.Count))
    [void]$sb.AppendLine(("| build/equiv issues | {0} |" -f $equivIssues.Count))
    [void]$sb.AppendLine("")

    [void]$sb.AppendLine("## Geomean Change By Impl")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("| impl | rows | geomean current/base | change |")
    [void]$sb.AppendLine("|------|-----:|---------------------:|-------:|")
    foreach ($impl in @($pairs | Select-Object -ExpandProperty impl -Unique | Sort-Object)) {
        $implPairs = @($pairs | Where-Object { $_.impl -eq $impl -and $_.change_ratio -and $_.change_ratio -gt 0 })
        if ($implPairs.Count -eq 0) {
            [void]$sb.AppendLine("| $impl | 0 | n/a | n/a |")
            continue
        }
        $logs = $implPairs | ForEach-Object { [Math]::Log($_.change_ratio) }
        $geo = [Math]::Exp(($logs | Measure-Object -Average).Average)
        [void]$sb.AppendLine(("| {0} | {1} | {2:F3}x | {3} |" -f $impl, $implPairs.Count, $geo, (Format-Change $geo)))
    }
    [void]$sb.AppendLine("")

    function Append-DeltaTable($title, $items) {
        [void]$sb.AppendLine("## $title")
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("| workload | impl | base ms | current ms | change |")
        [void]$sb.AppendLine("|----------|------|--------:|-----------:|-------:|")
        foreach ($item in $items) {
            $baseMs = if ($item.base_ns) { '{0:N1}' -f ($item.base_ns / 1e6) } else { 'n/a' }
            $curMs = if ($item.current_ns) { '{0:N1}' -f ($item.current_ns / 1e6) } else { 'n/a' }
            [void]$sb.AppendLine(("| {0} | {1} | {2} | {3} | {4} |" -f $item.workload, $item.impl, $baseMs, $curMs, (Format-Change $item.change_ratio)))
        }
        if (@($items).Count -eq 0) {
            [void]$sb.AppendLine("| none | — | — | — | — |")
        }
        [void]$sb.AppendLine("")
    }

    $topRegressions = @($pairs | Where-Object { $_.change_ratio } | Sort-Object change_ratio -Descending | Select-Object -First 10)
    $topImprovements = @($pairs | Where-Object { $_.change_ratio } | Sort-Object change_ratio | Select-Object -First 10)
    Append-DeltaTable "Largest Regressions" $topRegressions
    Append-DeltaTable "Largest Improvements" $topImprovements

    Set-Content -Path $MdPath -Value $sb.ToString() -Encoding UTF8
    return [pscustomobject]@{
        BaseWorkloads = $baseWorkloads.Count
        CurrentWorkloads = $curWorkloads.Count
        ComparableRows = $sharedRows
        CoverageIssueCount = $coverageIssueCount
        MissingCurrentRows = $missingCurrentRows.Count
        NewCurrentRows = $newCurrentRows.Count
        RegressionCount = $regressions.Count
        ImprovementCount = $improvements.Count
        BuildEquivIssueCount = $equivIssues.Count
        ReportPath = $MdPath
    }
}

if ($BaseCsv -or $CurrentCsv -or $MdPath) {
    if (-not $BaseCsv -or -not $CurrentCsv -or -not $MdPath) {
        Write-Error "Usage: compare.ps1 -BaseCsv <old.csv> -CurrentCsv <new.csv> -MdPath <out.md> [-FailOnRegression]"
        exit 2
    }
    $summary = Write-BenchmarkComparison `
        -BaseCsv $BaseCsv `
        -CurrentCsv $CurrentCsv `
        -MdPath $MdPath `
        -RegressionThreshold $RegressionThreshold `
        -ImprovementThreshold $ImprovementThreshold `
        -AllowPartialCoverage:$AllowPartialCoverage

    Write-Host ("Report -> {0}" -f $summary.ReportPath)
    Write-Host ("Comparable rows: {0}, coverage issues: {1}, regressions: {2}, build/equiv issues: {3}" -f $summary.ComparableRows, $summary.CoverageIssueCount, $summary.RegressionCount, $summary.BuildEquivIssueCount)

    if ($FailOnRegression -and (
        $summary.RegressionCount -gt 0 -or
        $summary.BuildEquivIssueCount -gt 0 -or
        ((-not $AllowPartialCoverage) -and $summary.CoverageIssueCount -gt 0)
    )) {
        exit 1
    }
}
