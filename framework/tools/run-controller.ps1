param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$LogDirectory
)
$ErrorActionPreference = 'Stop'
foreach ($path in @($Python, $Config, $LogDirectory)) {
    if (-not [IO.Path]::IsPathRooted($path) -or -not (Test-Path -LiteralPath $path)) {
        throw 'Provisioned absolute runtime, configuration and log paths required'
    }
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$entry = Join-Path $PSScriptRoot 'controller_entry.py'
# Windows PowerShell treats native stderr as an ErrorRecord. Do not terminate
# on the first traceback line before it can reach the protected error log.
$ErrorActionPreference = 'Continue'
& $Python -u -B -X utf8 $entry --config $Config 1>> (Join-Path $LogDirectory 'controller-service.out') 2>> (Join-Path $LogDirectory 'controller-service.err')
exit $LASTEXITCODE
