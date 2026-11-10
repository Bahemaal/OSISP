param([string]$Executable = (Join-Path $PSScriptRoot '..\build\LR4_Variant22.exe'))
$ErrorActionPreference='Stop'
$dir=Join-Path $PSScriptRoot '..\data\tests'
New-Item -ItemType Directory -Force $dir | Out-Null
$cases=@(
    @{Name='global-cap1'; Args=@('--mode','global','--capacity','1','--operations','200','--quiet'); Exit=0; Pattern='violations=0'},
    @{Name='stage-cap1'; Args=@('--mode','stage','--capacity','1','--workers','4','--operations','200','--quiet'); Exit=0; Pattern='completed=200'},
    @{Name='one-request'; Args=@('--mode','stage','--operations','1','--workers','4','--quiet'); Exit=0; Pattern='completed=1'},
    @{Name='individual'; Args=@('--individual','--mode','stage','--quiet'); Exit=0; Pattern='completed=1045'},
    @{Name='failure'; Args=@('--inject-failure','10','--quiet'); Exit=1; Pattern='INJECTED_WORKER_FAILURE'},
    @{Name='deadline'; Args=@('--operations','10000','--work','1000000','--max-ms','10','--quiet'); Exit=1; Pattern='EXPERIMENT_TIMEOUT'},
    @{Name='invalid-workers'; Args=@('--workers','0'); Exit=1; Pattern='positive parameters'},
    @{Name='invalid-mode'; Args=@('--mode','none'); Exit=1; Pattern='invalid mode'},
    @{Name='unknown-option'; Args=@('--anything','5'); Exit=1; Pattern='unknown option'}
)
$rows=@()
foreach($case in $cases){
    $argsForExe=$case.Args
    # Windows PowerShell 5 treats redirected native stderr as ErrorRecord.
    $savedPreference=$ErrorActionPreference
    $ErrorActionPreference='Continue'
    $output=& $Executable @argsForExe 2>&1
    $code=$LASTEXITCODE
    $ErrorActionPreference=$savedPreference
    $output | Set-Content (Join-Path $dir ($case.Name+'.txt')) -Encoding UTF8
    if($code -ne $case.Exit -or ($output -join "`n") -notmatch $case.Pattern){throw "FAILED: $($case.Name), exit=$code"}
    $rows += [pscustomobject]@{Test=$case.Name;ExitCode=$code;Result='PASS'}
}
$rows|Export-Csv (Join-Path $dir 'results.csv') -NoTypeInformation -Encoding UTF8
$rows|Format-Table
