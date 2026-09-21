[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$UserSid,
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [Parameter(Mandatory = $true)][string]$AppScript,
    [string]$StartupShortcut = ''
)

$ErrorActionPreference = 'Stop'
try {
    $pythonPath = (Resolve-Path -LiteralPath $PythonExecutable).ProviderPath
    $scriptPath = (Resolve-Path -LiteralPath $AppScript).ProviderPath
    $appDirectory = [System.IO.Path]::GetDirectoryName($scriptPath)
    $sid = New-Object System.Security.Principal.SecurityIdentifier($UserSid)
    $null = $sid.Translate([System.Security.Principal.NTAccount])

    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    $folder = $service.GetFolder('\')
    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = 'Chinese-English popup translator with automatic recovery'
    $definition.Principal.UserId = $UserSid
    $definition.Principal.LogonType = 3
    $definition.Principal.RunLevel = 0
    $definition.Settings.Enabled = $true
    $definition.Settings.AllowDemandStart = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.ExecutionTimeLimit = 'PT0S'
    $definition.Settings.MultipleInstances = 2
    $definition.Settings.StartWhenAvailable = $true
    $definition.Settings.RestartCount = 3
    $definition.Settings.RestartInterval = 'PT1M'
    $definition.Settings.WakeToRun = $false

    $logon = $definition.Triggers.Create(9)
    $logon.UserId = $UserSid
    $logon.Enabled = $true
    $recovery = $definition.Triggers.Create(1)
    $recovery.StartBoundary = (Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
    $recovery.Repetition.Interval = 'PT1M'
    $recovery.Repetition.StopAtDurationEnd = $false
    $recovery.Enabled = $true

    $action = $definition.Actions.Create(0)
    $action.Path = $pythonPath
    $action.Arguments = '"' + $scriptPath + '" --scheduled'
    $action.WorkingDirectory = $appDirectory
    $registered = $folder.RegisterTaskDefinition('zh-en-bilingual', $definition, 6, $UserSid, $null, 3, $null)
    if (-not $registered.Enabled) { throw 'The registered task is disabled.' }

    # Replace the previous startup shortcut only after registration succeeds.
    if ($StartupShortcut -and (Test-Path -LiteralPath $StartupShortcut)) {
        $shortcutPath = (Resolve-Path -LiteralPath $StartupShortcut).ProviderPath
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        if ($shortcut.TargetPath -eq $pythonPath -and $shortcut.Arguments.Contains($scriptPath)) {
            Remove-Item -LiteralPath $shortcutPath
        }
    }
    $null = $registered.Run($null)
    Write-Output 'Installed and started task: zh-en-bilingual (interactive user, limited privileges, recovery every minute).'
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
