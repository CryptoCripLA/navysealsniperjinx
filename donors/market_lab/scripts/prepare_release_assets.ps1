$ErrorActionPreference = "Stop"

$RawRoot = $env:PUMPFUN_RAW_ROOT
if ([string]::IsNullOrWhiteSpace($RawRoot)) {
    throw "Set PUMPFUN_RAW_ROOT to the PumpFunData raw directory before running this script."
}
$OutRoot = Join-Path $PSScriptRoot "..\release_assets\full_day_raw"
$StartUtc = [datetime]"2026-07-30T16:00:00Z"
$Datasets = @("pump_fun", "pump_amm")

New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null

foreach ($dataset in $Datasets) {
    for ($i = 0; $i -lt 24; $i++) {
        $dt = $StartUtc.AddHours($i)
        $date = $dt.ToString("yyyy-MM-dd")
        $hh = $dt.ToString("HH")
        $src = Join-Path $RawRoot "$dataset\date=$date\${dataset}_${date}_${hh}.parquet"
        if (!(Test-Path -LiteralPath $src)) {
            throw "Missing raw file: $src"
        }
        $destDir = Join-Path $OutRoot "$dataset\date=$date"
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
        $dest = Join-Path $destDir (Split-Path $src -Leaf)
        if (!(Test-Path -LiteralPath $dest)) {
            try {
                New-Item -ItemType HardLink -Path $dest -Target $src -ErrorAction Stop | Out-Null
            }
            catch {
                Copy-Item -LiteralPath $src -Destination $dest
            }
        }
    }
}

Write-Host "Prepared release assets at: $OutRoot"
