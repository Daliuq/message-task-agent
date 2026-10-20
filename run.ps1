[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LocalPython = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $LocalPython) {
    $PythonPath = $LocalPython
} else {
    $PythonPath = (Get-Command python -ErrorAction Stop).Source
}
$env:PYTHONPATH = Join-Path $Root 'src'
$env:PYTHONIOENCODING = 'utf-8'
& $PythonPath -m taskagent @CliArgs
exit $LASTEXITCODE
