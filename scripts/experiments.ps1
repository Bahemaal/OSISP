param([string]$Executable = (Join-Path $PSScriptRoot '..\build\LR4_Variant22.exe'))
$ErrorActionPreference='Stop'
$dir=Join-Path $PSScriptRoot '..\data'
New-Item -ItemType Directory -Force (Join-Path $dir 'runs') | Out-Null
$rows=@()
foreach($load in @('standard','individual')){
    $operations=500; $seed=1000
    if($load -eq 'individual'){$operations=1045;$seed=11573}
    for($repeat=1;$repeat -le 3;$repeat++){
        # Alternate mode order to reduce a fixed-order scheduling bias.
        $modes=@('global','stage'); if($repeat -eq 2){$modes=@('stage','global')}
        foreach($workers in @(2,4)){foreach($mode in $modes){
            $name="$load-$mode-$workers-$repeat"
            $out=& $Executable --mode $mode --workers $workers --operations $operations --seed $seed --quiet
            if($LASTEXITCODE -ne 0){throw "Experiment failed: $name"}
            $out|Set-Content (Join-Path $dir "runs\$name.txt") -Encoding UTF8
            $summary=$out|Where-Object {$_ -like 'SUMMARY *'}
            if(!$summary){throw "No summary: $name"}
            $row=[ordered]@{load=$load;repeat=$repeat}
            foreach($match in [regex]::Matches($summary,'(\w+)=([^\s]+)')){$row[$match.Groups[1].Value]=$match.Groups[2].Value}
            if($row.violations -ne '0' -or $row.completed -ne "$operations"){throw "Invariant failure: $name"}
            $rows += [pscustomobject]$row
            Write-Host "$name : $($row.elapsed_ms) ms, stats wait $($row.mean_stats_wait_us) us"
        }}
    }
}
$rows|Export-Csv (Join-Path $dir 'experiments.csv') -NoTypeInformation -Encoding UTF8
