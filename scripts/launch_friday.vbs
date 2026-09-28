' Silent wrapper for launch_friday.ps1, used by the "Launch FRIDAY" desktop
' shortcut. Runs PowerShell fully hidden (no console flash), which in turn
' starts Ollama (if needed) and FRIDAY's own START.bat.

Dim fso, shell, scriptDir, ps1Path, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1Path = scriptDir & "\launch_friday.ps1"

Set shell = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1Path & """"
shell.Run cmd, 0, False
