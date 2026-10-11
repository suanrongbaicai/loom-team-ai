param([Parameter(Mandatory=$true)][string]$JobFile, [Parameter(Mandatory=$true)][string]$CredentialFile, [switch]$Headless)
$ErrorActionPreference='Stop'
$ancJob=Get-Content -LiteralPath $JobFile -Raw -Encoding UTF8 | ConvertFrom-Json
$ancCredentials=Get-Content -LiteralPath $CredentialFile -Raw -Encoding UTF8 | ConvertFrom-Json
$ancAccount=$ancCredentials.accounts.($ancJob.os_account)
if(-not $ancAccount -or $ancJob.id -notmatch '^[a-zA-Z0-9_-]{1,96}$'){throw 'Job/account binding invalid'}
$ancPython=$ancCredentials.python
$ancEntry=Join-Path $PSScriptRoot 'worker_entry.py'
foreach($ancPath in @($JobFile,$ancEntry,$ancPython)){if($ancPath.Contains('"')){throw 'Invalid path'}}
# Codex's native Windows sandbox requires WinSta0. A password scheduled task
# creates a Service-* window station where sandbox children fail 0xC0000142.
# Run the fixed, administrator-owned worker under the scope's ordinary identity.
# The controller must itself be hosted in the logged-on desktop session.
if(-not $Headless -and (Get-Process -Id $PID).SessionId -eq 0){throw 'Desktop session unavailable; keep task queued until Windows sign-in'}
$ancCredential=New-Object System.Management.Automation.PSCredential($ancAccount.username,(ConvertTo-SecureString $ancAccount.password -AsPlainText -Force))
if($ancJob.worker_home){
    $ancHome=[IO.Path]::GetFullPath($ancJob.worker_home).TrimEnd('\')+'\'
    foreach($ancDirectory in @($ancJob.workspace,$ancJob.environment.TEMP)){
        $ancAbsolute=[IO.Path]::GetFullPath($ancDirectory)
        if(-not $ancAbsolute.StartsWith($ancHome,[StringComparison]::OrdinalIgnoreCase)){throw 'Task directory outside worker home'}
        $ancWalk=[IO.DirectoryInfo]$ancAbsolute
        while($ancWalk){
            if($ancWalk.Exists -and ($ancWalk.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Linked task directory refused'}
            $ancWalk=$ancWalk.Parent
        }
        [IO.Directory]::CreateDirectory($ancAbsolute) | Out-Null
        & icacls.exe $ancAbsolute /setowner ('*'+$ancAccount.sid) | Out-Null
        if($LASTEXITCODE -ne 0){throw 'Task directory ownership failed'}
    }
}
$ancOutput=[IO.Directory]::CreateDirectory($ancJob.output_dir).FullName
if($Headless){
    if($ancJob.desktop_container){
        $ancRoot=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
        $ancDesktopDirectory=Join-Path $ancRoot 'Control\DesktopHosts'
        [IO.Directory]::CreateDirectory($ancDesktopDirectory) | Out-Null
        $ancDesktopTask='LOOM-Desktop-'+$ancJob.id
        if(Get-ScheduledTask -TaskName $ancDesktopTask -ErrorAction SilentlyContinue){throw 'Desktop task already exists'}
        $ancDesktopEntry=Join-Path $PSScriptRoot 'desktop_host.py'
        $ancDesktopAction=New-ScheduledTaskAction -Execute $ancPython -Argument ('-B -X utf8 "'+$ancDesktopEntry+'" "'+$JobFile+'" "'+$ancAccount.sid+'"') -WorkingDirectory $PSScriptRoot
        $ancDesktopSettings=New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds ([int]$ancJob.timeout_seconds+60)) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $ancDesktopTask -Action $ancDesktopAction -Settings $ancDesktopSettings -User 'SYSTEM' -RunLevel Highest | Out-Null
        Start-ScheduledTask -TaskName $ancDesktopTask
        $ancReady=Join-Path $ancDesktopDirectory ($ancJob.id+'.json')
        $ancDeadline=[DateTime]::UtcNow.AddSeconds(12)
        while(-not (Test-Path -LiteralPath $ancReady) -and [DateTime]::UtcNow -lt $ancDeadline){Start-Sleep -Milliseconds 200}
        if(-not (Test-Path -LiteralPath $ancReady)){throw 'Private desktop readiness unavailable'}
        $ancDesktopResult=Get-Content -LiteralPath $ancReady -Raw | ConvertFrom-Json
        if($ancDesktopResult.state -ne 'ready' -or $ancDesktopResult.id -ne $ancJob.id){throw 'Private desktop creation failed'}
    }
    $ancTaskName='LOOM-Job-'+$ancJob.id
    if(Get-ScheduledTask -TaskName $ancTaskName -ErrorAction SilentlyContinue){throw 'Execution task already exists; inspect its receipt before any retry'}
    $ancAction=New-ScheduledTaskAction -Execute $ancPython -Argument ('-B -X utf8 "'+$ancEntry+'" "'+$JobFile+'"') -WorkingDirectory $ancJob.workspace
    $ancSettings=New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds ([int]$ancJob.timeout_seconds+90)) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $ancTaskName -Action $ancAction -Settings $ancSettings -User $ancAccount.username -Password $ancAccount.password -RunLevel Limited | Out-Null
    Start-ScheduledTask -TaskName $ancTaskName
    @{job=$ancJob.id;account=$ancAccount.username;task=$ancTaskName;state='registered'} | ConvertTo-Json -Compress
    exit
}
# Do not inherit the launcher's captured pipes: those handles would keep its
# caller waiting for EOF until the entire model process has exited.
$ancProcess=Start-Process -FilePath $ancPython -ArgumentList ('-B -X utf8 "'+$ancEntry+'" "'+$JobFile+'"') -WorkingDirectory $ancJob.workspace -Credential $ancCredential -LoadUserProfile -WindowStyle Hidden -RedirectStandardOutput (Join-Path $ancOutput 'worker.stdout') -RedirectStandardError (Join-Path $ancOutput 'worker.stderr') -PassThru
@{job=$ancJob.id;account=$ancAccount.username;pid=$ancProcess.Id;state='launched'} | ConvertTo-Json -Compress
