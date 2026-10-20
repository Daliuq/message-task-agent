[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^\d+$')][string]$Account,
    [string]$QQDataRoot = "$env:USERPROFILE\Documents\Tencent Files",
    [int]$WaitForExitSeconds = 180,
    [switch]$ConsentToLaunchQQ
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'PowerShell 7 required' }
if (-not $ConsentToLaunchQQ) { throw 'Explicit QQ launch consent is required' }
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$database = Join-Path $QQDataRoot "$Account\nt_qq\nt_db\nt_msg.db"
if (-not (Test-Path -LiteralPath $database -PathType Leaf)) { throw 'Account database not found' }
$privateDir = Join-Path $root 'data\qq'
[IO.Directory]::CreateDirectory($privateDir) | Out-Null
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $privateDir -AclObject $acl
$statusPath = Join-Path $privateDir 'key-setup-status.json'
function Set-Status([string]$state, $details=$null) {
    $status = @{state=$state;account_key=$Account;updated_at=[DateTimeOffset]::Now.ToString('o')}
    if ($details) { $status.details = $details }
    [IO.File]::WriteAllText($statusPath, ($status | ConvertTo-Json -Depth 6))
    Write-Output "QQ_KEY_SETUP:$state"
}
$key = $null
try {
    Set-Status 'waiting_for_qq_exit'
    $deadline = [DateTime]::UtcNow.AddSeconds($WaitForExitSeconds)
    while (Get-Process -Name QQ -ErrorAction SilentlyContinue) {
        if ([DateTime]::UtcNow -ge $deadline) { throw 'QQ exit wait expired' }
        Start-Sleep -Seconds 1
    }
    Set-Status 'launching_qq_waiting_for_login'
    $upstream = Join-Path $PSScriptRoot 'vendor\qq-win-db-key\scripts\windows\ntqq\windows_ntqq_get_key.ps1'
    # Upstream prints its key through Write-Host. Suppress ALL diagnostic streams;
    # capture only its structured return value, never serialize it to logs.
    $result = & $upstream 6>$null 5>$null 4>$null 3>$null 2>$null
    $keyResult = @($result | Where-Object { $_.PSObject.Properties['Key'] -and $_.Key })
    if ($keyResult.Count -ne 1) { throw 'No unique key returned' }
    $key = [string]$keyResult[0].Key
    if ($key.Length -ne 16) { throw 'Unexpected key length' }
    $secure = ConvertTo-SecureString -String $key -AsPlainText -Force
    $envelope = @{
        schema='taskagent.qq.key.dpapi.v1';account_key=$Account;
        protection='windows_dpapi_current_user';key_dpapi=($secure | ConvertFrom-SecureString);
        captured_at=[DateTimeOffset]::Now.ToString('o');verified=$false
    }
    $pending = Join-Path $privateDir "$Account.unverified.dpapi.json"
    [IO.File]::WriteAllText($pending, ($envelope | ConvertTo-Json -Depth 5))
    Set-Status 'key_captured_verifying_database'
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $python
    $start.ArgumentList.Add((Join-Path $PSScriptRoot 'verify_key.py'))
    $start.UseShellExecute=$false
    $start.CreateNoWindow=$true
    $start.RedirectStandardInput=$true
    $start.RedirectStandardOutput=$true
    $start.RedirectStandardError=$true
    $start.Environment['PYTHONIOENCODING']='utf-8'
    $worker = [Diagnostics.Process]::Start($start)
    $worker.StandardInput.Write((@{key=$key;database=$database} | ConvertTo-Json -Compress))
    $worker.StandardInput.Close()
    $stdoutTask=$worker.StandardOutput.ReadToEndAsync()
    $stderrTask=$worker.StandardError.ReadToEndAsync()
    if (-not $worker.WaitForExit(180000)) {
        $worker.Kill($true)
        throw 'Database verification timeout'
    }
    $verification = $stdoutTask.GetAwaiter().GetResult() | ConvertFrom-Json
    $null = $stderrTask.GetAwaiter().GetResult()
    if ($worker.ExitCode -ne 0 -or -not $verification.verified) {
        Set-Status 'captured_but_verification_failed'
        exit 2
    }
    $envelope.verified=$true
    $envelope.verification=$verification
    $destination=Join-Path $privateDir "$Account.dpapi.json"
    [IO.File]::WriteAllText($pending, ($envelope | ConvertTo-Json -Depth 6))
    Move-Item -LiteralPath $pending -Destination $destination -Force
    Set-Status 'verified_and_saved' $verification
}
catch {
    # Never print arbitrary upstream exceptions or partially captured objects.
    Set-Status 'failed' @{error_type=$_.Exception.GetType().Name}
    exit 1
}
finally {
    $key=$null; $keyResult=$null; $result=$null; $secure=$null
}
