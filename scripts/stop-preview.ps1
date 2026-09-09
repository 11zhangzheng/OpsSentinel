[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
$PidFile = Join-Path $ProjectRoot '.opssentinel\controller.pid'

if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
    Write-Host 'No background preview PID file exists; nothing to stop.'
    exit 0
}
$RecordedPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
$PreviewProcessId = 0
if ($RecordedPid -notmatch '^[1-9][0-9]*$' -or
    -not [int]::TryParse($RecordedPid, [ref]$PreviewProcessId)) {
    throw 'The preview PID file is invalid. No process was stopped.'
}

# Parse actual Windows argv, so quoted paths containing spaces remain one token.
if (-not ('OpsSentinelPreview.Arguments' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
namespace OpsSentinelPreview {
    public static class Arguments {
        [DllImport("shell32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CommandLineToArgvW(string commandLine, out int count);
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);
        public static string[] Split(string commandLine) {
            int count;
            IntPtr memory = CommandLineToArgvW(commandLine, out count);
            if (memory == IntPtr.Zero) throw new Win32Exception();
            try {
                string[] values = new string[count];
                for (int index = 0; index < count; index++)
                    values[index] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory, index * IntPtr.Size));
                return values;
            } finally { LocalFree(memory); }
        }
    }
}
'@
}

function Test-PreviewCommand {
    param($ProcessInfo)
    if (-not $ProcessInfo -or [string]::IsNullOrWhiteSpace($ProcessInfo.CommandLine)) {
        return $false
    }
    $ProcessName = [string]$ProcessInfo.Name
    if ($ProcessName -notmatch '^(python(?:w|[0-9]+(?:\.[0-9]+)*)?|py)\.exe$') {
        return $false
    }
    $Arguments = @([OpsSentinelPreview.Arguments]::Split($ProcessInfo.CommandLine))
    $HasProjectPath = $false
    $ModuleCount = 0
    $HostCount = 0
    $DemoCount = 0
    for ($Index = 0; $Index -lt $Arguments.Count; $Index++) {
        $Argument = $Arguments[$Index]
        if ([System.IO.Path]::IsPathRooted($Argument)) {
            try {
                $ArgumentPath = [System.IO.Path]::GetFullPath($Argument).TrimEnd('\', '/')
                if ($ArgumentPath.Equals($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -or
                    $ArgumentPath.StartsWith($ProjectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
                    $HasProjectPath = $true
                }
            } catch { return $false }
        }
        if ($Argument -ceq '-m') {
            $ModuleCount++
            if ($Index + 1 -ge $Arguments.Count -or $Arguments[$Index + 1] -cne 'opssentinel') {
                return $false
            }
        }
        if ($Argument -ceq '--host') {
            $HostCount++
            if ($Index + 1 -ge $Arguments.Count -or $Arguments[$Index + 1] -cne '127.0.0.1') {
                return $false
            }
        }
        if ($Argument -ceq '--demo') { $DemoCount++ }
        if ($Argument -ceq '--no-demo' -or $Argument.StartsWith('--host=') -or $Argument -ceq '-c') {
            return $false
        }
    }
    return $HasProjectPath -and $ModuleCount -eq 1 -and $HostCount -eq 1 -and $DemoCount -eq 1
}

function Stop-VerifiedPreviewProcess {
    param($ExpectedProcess)
    $Current = Get-CimInstance Win32_Process -Filter "ProcessId=$($ExpectedProcess.ProcessId)"
    if (-not $Current) { return }
    if ($Current.CreationDate -ne $ExpectedProcess.CreationDate -or
        $Current.ParentProcessId -ne $ExpectedProcess.ParentProcessId -or
        -not (Test-PreviewCommand $Current)) {
        throw "Process identity changed for PID $($ExpectedProcess.ProcessId); it was not stopped."
    }
    try {
        Stop-Process -Id $Current.ProcessId -ErrorAction Stop
    } catch {
        # The venv wrapper may exit as soon as its child exits.
        if (Get-CimInstance Win32_Process -Filter "ProcessId=$($Current.ProcessId)") { throw }
    }
}

# Read-only preflight: finish all ownership checks before stopping any process.
$Wrapper = Get-CimInstance Win32_Process -Filter "ProcessId=$PreviewProcessId"
if (-not $Wrapper) {
    Write-Host 'The recorded preview process has already exited; nothing was stopped.'
    exit 0
}
if (-not (Test-PreviewCommand $Wrapper)) {
    throw 'The recorded PID does not match this project and localhost demo command. No process was stopped.'
}
$Children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$PreviewProcessId" |
    Where-Object { $_.Name -match '^python(?:w|[0-9]+(?:\.[0-9]+)*)?\.exe$' })
foreach ($Child in $Children) {
    if (-not (Test-PreviewCommand $Child) -or $Child.CreationDate -lt $Wrapper.CreationDate) {
        throw 'A direct Python child does not match the preview command. No process was stopped.'
    }
}

if ($PSCmdlet.ShouldProcess("this project's localhost demo preview (PID $PreviewProcessId)", 'Stop verified child processes and then wrapper')) {
    foreach ($Child in $Children) { Stop-VerifiedPreviewProcess $Child }
    Stop-VerifiedPreviewProcess $Wrapper
    if ((Test-Path -LiteralPath $PidFile -PathType Leaf) -and
        (Get-Content -LiteralPath $PidFile -Raw).Trim() -ceq $RecordedPid) {
        Remove-Item -LiteralPath $PidFile
    }
    Write-Host 'The verified background preview has been stopped.'
}
