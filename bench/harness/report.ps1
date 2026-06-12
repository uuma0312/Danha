# report.ps1 — CSV → markdown report with ratio table + ASCII bar chart

function Write-MarkdownReport {
    param(
        [string]$CsvPath,
        [string]$MdPath,
        [string[]]$Impls
    )
    $rows = Import-Csv $CsvPath
    if ($rows.Count -eq 0) {
        Set-Content $MdPath -Value "# Empty report"
        return
    }

    $workloads = $rows | Select-Object -ExpandProperty workload -Unique
    $sb = [System.Text.StringBuilder]::new()

    [void]$sb.AppendLine("# Danha vs C++/C# Benchmark Report")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("Min wall-clock ms over (warmup discarded). ELAPSED_NS used when available.")
    [void]$sb.AppendLine("Ratio = impl_min / danha_min (lower = faster than Danha, > 1 = slower).")
    [void]$sb.AppendLine("")

    # Coverage and correctness summary
    $totalRows = @($rows).Count
    $buildFailures = @($rows | Where-Object { $_.build_ok -ne $true -and $_.build_ok -ne 'True' }).Count
    $equivFailures = @($rows | Where-Object { $_.equiv_ok -ne $true -and $_.equiv_ok -ne 'True' }).Count
    $missingTiming = @($rows | Where-Object { -not $_.min_ns }).Count

    [void]$sb.AppendLine("## Coverage")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("| metric | value |")
    [void]$sb.AppendLine("|--------|------:|")
    [void]$sb.AppendLine(("| workloads | {0} |" -f @($workloads).Count))
    [void]$sb.AppendLine(("| result rows | {0} |" -f $totalRows))
    [void]$sb.AppendLine(("| build failures | {0} |" -f $buildFailures))
    [void]$sb.AppendLine(("| equivalence failures | {0} |" -f $equivFailures))
    [void]$sb.AppendLine(("| missing timing rows | {0} |" -f $missingTiming))
    [void]$sb.AppendLine("")

    # Geometric mean of ratios per impl
    [void]$sb.AppendLine("## Summary — geometric mean ratio across workloads")
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("| impl | geomean(impl/danha) |")
    [void]$sb.AppendLine("|------|---------------------|")
    foreach ($impl in $Impls) {
        $ratios = @($rows | Where-Object { $_.impl -eq $impl -and $_.ratio_vs_danha } | ForEach-Object { [double]$_.ratio_vs_danha })
        if ($ratios.Count -gt 0) {
            $logs = $ratios | ForEach-Object { [Math]::Log($_) }
            $geo = [Math]::Exp(($logs | Measure-Object -Average).Average)
            [void]$sb.AppendLine(("| {0} | {1:F3}x |" -f $impl, $geo))
        } else {
            [void]$sb.AppendLine("| $impl | n/a |")
        }
    }
    [void]$sb.AppendLine("")

    # Per-workload table + bar chart
    foreach ($wl in $workloads) {
        [void]$sb.AppendLine("## $wl")
        [void]$sb.AppendLine("")
        $wlRows = $rows | Where-Object { $_.workload -eq $wl }
        # Find max ms for bar scale
        $maxNs = ($wlRows | Where-Object { $_.min_ns } | Measure-Object -Property min_ns -Maximum).Maximum
        if (-not $maxNs) { $maxNs = 1 }

        [void]$sb.AppendLine("| impl | min ms | ratio | equiv | bar |")
        [void]$sb.AppendLine("|------|-------:|------:|:-----:|-----|")
        foreach ($impl in $Impls) {
            $r = $wlRows | Where-Object { $_.impl -eq $impl } | Select-Object -First 1
            if (-not $r) {
                [void]$sb.AppendLine("| $impl | — | — | — | — |")
                continue
            }
            $ms = if ($r.min_ns) { '{0:N1}' -f ([double]$r.min_ns / 1e6) } else { '—' }
            $ratio = if ($r.ratio_vs_danha) { '{0:F2}x' -f [double]$r.ratio_vs_danha } else { '—' }
            $eq = if ($r.equiv_ok -eq $true -or $r.equiv_ok -eq 'True') { '✓' } else { '✗' }
            $barLen = 0
            if ($r.min_ns) { $barLen = [int](([double]$r.min_ns / $maxNs) * 30) }
            $bar = ('█' * $barLen).PadRight(30)
            [void]$sb.AppendLine("| $impl | $ms | $ratio | $eq | ``$bar`` |")
        }
        [void]$sb.AppendLine("")
    }

    Set-Content -Path $MdPath -Value $sb.ToString() -Encoding UTF8
}
