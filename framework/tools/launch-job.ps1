param([Parameter(Mandatory=$true)][string]$JobFile, [Parameter(Mandatory=$true)][string]$CredentialFile, [switch]$Headless)
$ErrorActionPreference='Stop'
$loomJob=Get-Content -LiteralPath $JobFile -Raw -Encoding UTF8 | ConvertFrom-Json
$loomCredentials=Get-Content -LiteralPath $CredentialFile -Raw -Encoding UTF8 | ConvertFrom-Json
$loomAccount=$loomCredentials.accounts.($loomJob.os_account)
if(-not $loomAccount -or $loomJob.id -notmatch '^[a-zA-Z0-9_-]{1,96}$'){throw 'Job/account binding invalid'}
$loomPython=$loomCredentials.python
$loomEntry=Join-Path $PSScriptRoot 'worker_entry.py'
foreach($loomPath in @($JobFile,$loomEntry,$loomPython)){if($loomPath.Contains('"')){throw 'Invalid path'}}
# Codex's native Windows sandbox requires WinSta0. A password scheduled task
# creates a Service-* window station where sandbox children fail 0xC0000142.
# Run the fixed, administrator-owned worker under the scope's ordinary identity.
# The controller must itself be hosted in the logged-on desktop session.
if(-not $Headless -and (Get-Process -Id $PID).SessionId -eq 0){throw 'Desktop session unavailable; keep task queued until Windows sign-in'}
$loomCredential=New-Object System.Management.Automation.PSCredential($loomAccount.username,(ConvertTo-SecureString $loomAccount.password -AsPlainText -Force))
if($loomJob.worker_home){
    $loomHome=[IO.Path]::GetFullPath($loomJob.worker_home).TrimEnd('\')+'\'
    foreach($loomDirectory in @($loomJob.workspace,$loomJob.environment.TEMP)){
        $loomAbsolute=[IO.Path]::GetFullPath($loomDirectory)
        if(-not $loomAbsolute.StartsWith($loomHome,[StringComparison]::OrdinalIgnoreCase)){throw 'Task directory outside worker home'}
        $loomWalk=[IO.DirectoryInfo]$loomAbsolute
        while($loomWalk){
            if($loomWalk.Exists -and ($loomWalk.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Linked task directory refused'}
            $loomWalk=$loomWalk.Parent
        }
        [IO.Directory]::CreateDirectory($loomAbsolute) | Out-Null
        & icacls.exe $loomAbsolute /setowner ('*'+$loomAccount.sid) | Out-Null
        if($LASTEXITCODE -ne 0){throw 'Task directory ownership failed'}
    }
}
$loomOutput=[IO.Directory]::CreateDirectory($loomJob.output_dir).FullName
if($Headless){
    if($loomJob.desktop_container){
        $loomRoot=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
        $loomDesktopDirectory=Join-Path $loomRoot 'Control\DesktopHosts'
        [IO.Directory]::CreateDirectory($loomDesktopDirectory) | Out-Null
        $loomDesktopTask='LOOM-Desktop-'+$loomJob.id
        if(Get-ScheduledTask -TaskName $loomDesktopTask -ErrorAction SilentlyContinue){throw 'Desktop task already exists'}
        $loomDesktopEntry=Join-Path $PSScriptRoot 'desktop_host.py'
        $loomDesktopAction=New-ScheduledTaskAction -Execute $loomPython -Argument ('-B -X utf8 "'+$loomDesktopEntry+'" "'+$JobFile+'" "'+$loomAccount.sid+'"') -WorkingDirectory $PSScriptRoot
        $loomDesktopSettings=New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds ([int]$loomJob.timeout_seconds+60)) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $loomDesktopTask -Action $loomDesktopAction -Settings $loomDesktopSettings -User 'SYSTEM' -RunLevel Highest | Out-Null
        Start-ScheduledTask -TaskName $loomDesktopTask
        $loomReady=Join-Path $loomDesktopDirectory ($loomJob.id+'.json')
        $loomDeadline=[DateTime]::UtcNow.AddSeconds(12)
        while(-not (Test-Path -LiteralPath $loomReady) -and [DateTime]::UtcNow -lt $loomDeadline){Start-Sleep -Milliseconds 200}
        if(-not (Test-Path -LiteralPath $loomReady)){throw 'Private desktop readiness unavailable'}
        $loomDesktopResult=Get-Content -LiteralPath $loomReady -Raw | ConvertFrom-Json
        if($loomDesktopResult.state -ne 'ready' -or $loomDesktopResult.id -ne $loomJob.id){throw 'Private desktop creation failed'}
    }
    $loomTaskName='LOOM-Job-'+$loomJob.id
    if(Get-ScheduledTask -TaskName $loomTaskName -ErrorAction SilentlyContinue){throw 'Execution task already exists; inspect its receipt before any retry'}
    $loomAction=New-ScheduledTaskAction -Execute $loomPython -Argument ('-B -X utf8 "'+$loomEntry+'" "'+$JobFile+'"') -WorkingDirectory $loomJob.workspace
    $loomSettings=New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds ([int]$loomJob.timeout_seconds+90)) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $loomTaskName -Action $loomAction -Settings $loomSettings -User $loomAccount.username -Password $loomAccount.password -RunLevel Limited | Out-Null
    Start-ScheduledTask -TaskName $loomTaskName
    @{job=$loomJob.id;account=$loomAccount.username;task=$loomTaskName;state='registered'} | ConvertTo-Json -Compress
    exit
}
# Do not inherit the launcher's captured pipes: those handles would keep its
# caller waiting for EOF until the entire model process has exited.
$loomProcess=Start-Process -FilePath $loomPython -ArgumentList ('-B -X utf8 "'+$loomEntry+'" "'+$JobFile+'"') -WorkingDirectory $loomJob.workspace -Credential $loomCredential -LoadUserProfile -WindowStyle Hidden -RedirectStandardOutput (Join-Path $loomOutput 'worker.stdout') -RedirectStandardError (Join-Path $loomOutput 'worker.stderr') -PassThru
@{job=$loomJob.id;account=$loomAccount.username;pid=$loomProcess.Id;state='launched'} | ConvertTo-Json -Compress
