"""
Windows Fixer AI  -  Python + Tkinter GUI that uses OpenCode Zen free models
or NVIDIA NIM models (build.nvidia.com) to diagnose Windows problems and
suggest / run fix commands.

- Paste your API key in the app (saved on this PC), or click "Auto-detect key
  on this PC" to find keys in environment variables / OpenCode's login file.
- Detects your NVIDIA GPU (nvidia-smi) and shares it with the AI as context.
- Repair-loop fix with AI: pick ANY drive that holds Windows - the one running now, or a broken PC's drive
  attached to this PC - and the AI repairs it in a loop (diagnose -> run safe commands -> read output -> repeat).
- Small Auto-test screen at the top: tests the Windows (or other OS) on the drive being fixed before and after
  each AI fix and shows what changed.
- VM FIRST: before any AI fix starts, the drive is booted in the VM and the app reads the screen (Windows' built-in OCR),
  so the AI knows the real symptom (boot loop, blue screen + stop code, black screen ...) before it chooses fixes.
- Virtual-machine screen next to it: boots the drive being fixed in a throw-away QEMU VM (read-only snapshot,
  nothing is written to the disk, no network) and shows the live screen, before and after each fix.
- FULL PC SCAN: one click scans the WHOLE PC read-only (system files, disk, network, Windows Update, services, drivers,
  event logs, crashes, Defender, startup, printers, clock ...). "Scan + auto-fix everything" lets the AI fix what it finds,
  round after round, verifying each fix; a restore point is created first.
- 55 one-click quick fixes in categories (network, updates, system files, slow PC, Start menu/apps, drivers, security).

No third-party packages needed (only the Python standard library).
Run:  python windows_fixer.py
"""

import ctypes
import json
import os
import platform
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

PROVIDERS = {
    "opencode": {
        "label": "OpenCode Zen (free)",
        "base": "https://opencode.ai/zen/v1",
        "default_key": "public",  # shared free key; replace with your own Zen key if needed
        "env": ["OPENCODE_API_KEY"],
        "fallback": ["big-pickle", "mimo-v2.5-free", "deepseek-v4-flash-free", "nemotron-3-ultra-free"],
        "key_url": "https://opencode.ai/auth",
    },
    "nvidia": {
        "label": "NVIDIA NIM (build.nvidia.com)",
        "base": "https://integrate.api.nvidia.com/v1",
        "default_key": "",  # you must paste your own nvapi-... key
        "env": ["NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "NGC_API_KEY"],
        "fallback": [
            "deepseek-ai/deepseek-v3.2",
            "deepseek-ai/deepseek-v3.1",
            "meta/llama-3.3-70b-instruct",
            "nvidia/llama-3.3-nemotron-super-49b-v1",
            "meta/llama-3.1-405b-instruct",
            "qwen/qwen2.5-coder-32b-instruct",
        ],  # used if the live list can't be fetched; dead ones are skipped automatically
        "key_url": "https://build.nvidia.com/settings/api-keys",
    },
}
DEFAULT_PROVIDER = "opencode"

# NVIDIA's catalog also holds embedding / vision / safety / image models; hide those (this app is text chat).
NV_SKIP = ("embed", "rerank", "guard", "safety", "reward", "parse", "clip", "vila", "neva", "paligemma",
           "kosmos", "fuyu", "vision", "-vl", "vlm", "tts", "asr", "riva", "retriev", "bge", "diffusion",
           "stable", "sdxl", "flux", "cosmos", "genmol", "molmim", "esm", "fastpitch", "maxine", "deplot",
           "gliner", "pii", "topic-control")

SYSTEM_PROMPT = (
    "You are an expert Windows 10/11 technician who can fix ANY Windows problem: boot/startup, blue screens, slow PC, disk and "
    "storage, network/Wi-Fi/internet, Windows Update, corrupted files, apps that crash or will not install, Microsoft Store, "
    "Start menu/taskbar/Explorer, drivers and devices (audio, display, Bluetooth, USB, printers), services, security/Defender/"
    "firewall, clock/time, activation and settings. The user describes a problem. "
    "Diagnose the likely causes, then give a safe step-by-step fix.\n"
    "Rules:\n"
    "- Prefer the least risky fix first (restart service, SFC, DISM, driver/network reset).\n"
    "- Put every command inside a ```cmd code block, ONE command per line, runnable in cmd.exe. "
    "For PowerShell wrap it as: powershell -NoProfile -Command \"...\".\n"
    "- Never include destructive commands (format, diskpart clean, del /s on system folders, "
    "registry deletes) unless absolutely required, and warn clearly if you do.\n"
    "- Mention when a step needs Administrator rights or a reboot.\n"
    "- If the cause is unclear, start with a short READ-ONLY diagnostic command block, then fix once you know more.\n"
    "- If a fix is not possible by command (failing hardware, vendor driver, reinstall), say so plainly and give exact manual steps.\n"
    "- Keep answers concise."
)

RISKY = [
    r"\bformat\b\s+[a-z]:", r"diskpart", r"\bdel\b.*\s/s", r"\brd\b\s+/s", r"\brmdir\b\s+/s",
    r"remove-item.*-recurse", r"reg(\.exe)?\s+delete", r"bcdedit", r"cipher\s+/w",
    r"clear-disk", r"remove-partition", r"net\s+user", r"takeown.*\\windows",
]

# Auto-fix mode runs commands WITHOUT asking each time, so it blocks anything RISKY plus power/boot/account actions.
AUTO_BLOCK = RISKY + [r"\bshutdown\b", r"restart-computer", r"stop-computer", r"\bnet\s+localgroup\b",
                      r"remove-item", r"\bformat-", r"\bsc\s+delete\b", r"vssadmin\s+delete", r"wmic.*delete",
                      r"set-executionpolicy", r"disable-windowsoptionalfeature"]
AUTOFIX_MAX_ROUNDS = 6
AUTOFIX_RULES = (
    "[AUTO-FIX MODE] The app will RUN the commands you give automatically and send you the output.\n"
    "- Running as Administrator: {admin}\n"
    "- Each reply: 1-2 sentences of reasoning, then ONE ```cmd block with at most 5 commands (one per line). "
    "Commands run non-interactively (no prompts - add /y, -y, /quiet where needed) and must finish within 30 minutes.\n"
    "- Start with read-only diagnostics, then apply the least risky fix. Never use destructive, reboot or shutdown "
    "commands - they are blocked. If a reboot is required, say so in plain text instead.\n"
    "- When the problem is fixed or nothing more can be done automatically, reply WITHOUT a code block and end with "
    "FIX_DONE (fixed) or NEED_USER: <what the user must do manually>."
)

DISK_RULES = (
    "\n\n[DRIVE REPAIR] Target drive: {L}: ONLY. Any command that touches another drive letter is blocked.\n"
    "- The first message contains read-only health checks for {L}:. Fix only real problems you can see in them "
    "(file system errors, dirty bit, bad sectors, offline/RAW volume). If the disk is healthy, say so and finish with FIX_DONE.\n"
    "- Preferred order: Repair-Volume -DriveLetter {L} -Scan (PowerShell: powershell -NoProfile -Command \"...\"), "
    "then  echo Y| chkdsk {L}: /f  (the 'echo Y|' answers the schedule/dismount prompt). "
    "If chkdsk says it is scheduled for the next restart, report NEED_USER: restart the PC.\n"
    "- Use chkdsk /r (bad-sector scan) only if the checks show bad sectors or read/write errors, and warn that it can take hours "
    "(a command is killed after 30 minutes - if that is likely, give NEED_USER instead).\n"
    "- If SMART/health shows the disk is physically failing, do NOT keep repairing: tell the user to back up and replace it "
    "(NEED_USER). Never format, repartition or delete data."
)
_DRIVE_TOOLS = re.compile(r"\b(chkdsk|repair-volume|defrag|optimize-volume|fsutil|convert|label|vol)\b", re.I)


def _drive_letters(cmd):
    """Every drive letter a command mentions (E:\\..., -DriveLetter E, ...)."""
    letters = {m.upper() for m in re.findall(r"(?<![A-Za-z0-9])([A-Za-z]):(?![\\/]{2})", cmd)}
    letters |= {m.upper() for m in re.findall(r"-DriveLetter\s+['\"]?([A-Za-z])\b", cmd, re.I)}
    return letters


def drive_ok(cmd, L):
    """In drive-repair mode: a disk command must name the selected drive and no other drive letter."""
    if not _DRIVE_TOOLS.search(cmd):
        return True
    return _drive_letters(cmd) == {L.upper()}


# Offline mode = the Windows to repair is on ANOTHER drive (e.g. a broken PC's disk attached to this PC).
OFFLINE_RULES = (
    "\n\n[OFFLINE WINDOWS REPAIR] The Windows installation to repair is on drive {L}: and it is NOT the Windows that is "
    "running this app - it is an attached drive from a PC stuck in the Automatic Repair loop. Work ONLY on {L}:.\n"
    "- Never use /Online, plain 'sfc /scannow', reagentc, bcdboot, bootrec or bcdedit without /store: those would change the "
    "RUNNING Windows and are blocked.\n"
    "- Use the offline forms: mkdir {L}:\\scratch  then  DISM /Image:{L}:\\ /ScratchDir:{L}:\\scratch /Cleanup-Image "
    "/RevertPendingActions ;  sfc /scannow /offbootdir={L}:\\ /offwindir={L}:\\Windows ;  echo Y| chkdsk {L}: /f ;  "
    "ren \"{L}:\\Windows\\WinSxS\\pending.xml\" pending.xml.bak  (rename, never delete).\n"
    "- DISM /RestoreHealth cannot download files offline - do not use it. If DISM says the image is newer than this PC's DISM, "
    "stop and finish with NEED_USER: run the recovery-screen .bat from the broken PC's own recovery environment.\n"
    "- The boot configuration (BCD) of UEFI PCs lives on a hidden EFI partition that is not reachable here. Only if "
    "{L}:\\Boot\\BCD exists (legacy BIOS disk) you may back it up with  copy /y {L}:\\Boot\\BCD {L}:\\Boot\\BCD.bak  and then run  "
    "bcdedit /store {L}:\\Boot\\BCD /set {{default}} recoveryenabled No  (tell the user to undo it later with recoveryenabled Yes). "
    "bcdedit is allowed ONLY with /store {L}:\\Boot\\BCD and only /enum, /export, or /set {{default}} recoveryenabled|"
    "bootstatuspolicy|bootmenupolicy <value>.\n"
    "- Read the diagnostics (SrtTrail.txt, CBS.log, pending.xml, minidumps, that PC's System event log, BitLocker/disk health) "
    "and fix the most likely cause. Do not delete files.\n"
    "- When finished, say NEED_USER: put the drive back in its PC and try to boot (and tell me if it still loops)."
)
_OFFLINE_BCD = re.compile(
    r'^bcdedit(?:\.exe)?\s+/store\s+"?[A-Za-z]:\\[\w\\.\- ]+"?\s+'
    r'(?:/enum(?:\s+(?:all|\{\w+\}))?'
    r'|/export\s+"?[A-Za-z]:\\[\w\\.\- ]+"?'
    r'|/set\s+\{(?:default|current)\}\s+(?:recoveryenabled|bootstatuspolicy|bootmenupolicy)\s+\w+)\s*$', re.I)


def offline_ok(cmd, L):
    """Offline repair mode: every command must act on drive L: itself and nothing else - never the running Windows,
    another drive, or a second chained command."""
    c, L = cmd.strip(), L.upper()
    if re.search(r"&|\|\||>", c):   # no command chaining / redirection (a plain  echo Y| chkdsk  pipe is fine)
        return False
    if re.search(r"/online\b", c, re.I) or re.search(r"(?<![\w-])(reagentc|bcdboot|bootrec)(?![\w-])", c, re.I):
        return False
    if re.search(r"(?<![\w-])bcdedit(?![\w-])", c, re.I):
        return bool(_OFFLINE_BCD.match(c)) and _drive_letters(c) == {L}
    if re.search(r"(?<![\w-])sfc(?![\w-])", c, re.I) and not (
            re.search(r"/offbootdir=", c, re.I) and re.search(r"/offwindir=", c, re.I)):
        return False
    return _drive_letters(c) == {L}


WINRE_SCRIPT = r'''
@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Windows boot loop repair (Windows Fixer AI)
echo ==========================================================
echo  Windows boot loop repair
echo  Run from the recovery Command Prompt:
echo  Troubleshoot - Advanced options - Command Prompt
echo ==========================================================
echo.
set "LOG=%~dp0repair_log.txt"
echo Repair run %DATE% %TIME% > "%LOG%"

set "WIN="
for %%D in (C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
  if exist "%%D:\Windows\System32\Config\SYSTEM" if not defined WIN set "WIN=%%D:"
)
if not defined WIN (
  echo Could not find an installed Windows.
  echo If the drive is protected by BitLocker, unlock it first:
  echo    manage-bde -unlock C: -RecoveryPassword YOUR-48-DIGIT-KEY
  echo then run this script again.
  goto :end
)
echo [1/7] Windows found on %WIN%
echo [1/7] Windows found on %WIN% >> "%LOG%"

echo.
echo [2/7] Checking and repairing the file system on %WIN% (can take a long time)...
chkdsk %WIN% /f
echo chkdsk exit code !errorlevel! >> "%LOG%"

echo.
echo [3/7] Undoing half-installed Windows updates...
if not exist "%WIN%\scratch" mkdir "%WIN%\scratch"
dism /Image:%WIN%\ /ScratchDir:%WIN%\scratch /Cleanup-Image /RevertPendingActions
echo dism revert exit code !errorlevel! >> "%LOG%"
if exist "%WIN%\Windows\WinSxS\pending.xml" (
  ren "%WIN%\Windows\WinSxS\pending.xml" pending.xml.bak
  echo renamed pending.xml >> "%LOG%"
)

echo.
echo [4/7] Checking Windows system files (SFC offline)...
sfc /scannow /offbootdir=%WIN%\ /offwindir=%WIN%\Windows
echo sfc exit code !errorlevel! >> "%LOG%"

echo.
echo [5/7] Rebuilding the boot files (BCD)...
set "EFI="
set "EFIVOL="
for /f "tokens=2" %%V in ('echo list volume ^| diskpart ^| findstr /i /c:"FAT32" ^| findstr /i /c:"System"') do set "EFIVOL=%%V"
if defined EFIVOL (
  (echo select volume !EFIVOL!& echo assign letter=Z) | diskpart >nul 2>&1
  if exist Z:\ set "EFI=Z:"
)
if defined EFI (
  echo UEFI system partition found on %EFI%
  copy /y "%EFI%\EFI\Microsoft\Boot\BCD" "%~dp0BCD_backup" >nul 2>&1
  bcdboot %WIN%\Windows /s %EFI% /f UEFI
  echo bcdboot UEFI exit code !errorlevel! >> "%LOG%"
) else (
  echo No UEFI system partition found - using legacy BIOS repair.
  echo Answer Y if it asks to add the installation to the boot list.
  bootrec /fixmbr
  bootrec /fixboot
  bootrec /scanos
  bootrec /rebuildbcd
  bcdboot %WIN%\Windows
  echo legacy repair done >> "%LOG%"
)

echo.
echo [6/7] Optional: turn OFF the automatic repair loop.
echo This makes Windows attempt a normal start instead of looping into Automatic Repair.
echo Undo later with:  bcdedit /set {default} recoveryenabled Yes
set /p ANS=Turn off automatic repair now? (Y/N): 
if /i "!ANS!"=="Y" (
  set "STORE="
  if defined EFI set "STORE=/store %EFI%\EFI\Microsoft\Boot\BCD"
  bcdedit !STORE! /set {default} recoveryenabled No
  bcdedit !STORE! /set {default} bootstatuspolicy ignoreallfailures
  echo automatic repair disabled >> "%LOG%"
)

echo.
echo [7/7] Saving the Startup Repair log next to this script...
if exist "%WIN%\Windows\System32\Logfiles\Srt\SrtTrail.txt" copy /y "%WIN%\Windows\System32\Logfiles\Srt\SrtTrail.txt" "%~dp0SrtTrail.txt" >nul
if exist "%WIN%\scratch" rmdir /s /q "%WIN%\scratch"

echo.
echo Finished. Type  exit  and choose Continue to restart into Windows.
echo If it still loops, open SrtTrail.txt with 'Analyze a boot log with AI' in Windows Fixer AI.
:end
echo.
pause
'''


def _pscmd(script):
    """A PowerShell one-liner that can be run from cmd.exe (the script must not contain double quotes)."""
    return 'powershell -NoProfile -Command "' + script + '"'


# Quick fixes by category. Every command runs in cmd.exe; most need Administrator.
QUICK_FIX_GROUPS = {
    "Network / Internet": {
        "Flush DNS cache": "ipconfig /flushdns",
        "Reset network (Winsock + IP)": "netsh winsock reset & netsh int ip reset",
        "Release / renew IP": "ipconfig /release & ipconfig /renew",
        "Re-register DNS": "ipconfig /registerdns",
        "Clear ARP cache": "netsh interface ip delete arpcache",
        "Reset proxy settings (WinHTTP)": "netsh winhttp reset proxy",
        "Restart network adapters": _pscmd(
            r"Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | Restart-NetAdapter -Confirm:$false"),
        "Restart Wi-Fi service (WLAN AutoConfig)": "net stop wlansvc /y & net start wlansvc",
        "Test internet connection": "ping -n 4 8.8.8.8 & ping -n 4 microsoft.com",
        "Show full network config": "ipconfig /all",
    },
    "Windows Update": {
        "Restart Windows Update service": "net stop wuauserv & net start wuauserv",
        "Reset Windows Update components (renames the cache folders)": (
            "net stop wuauserv & net stop cryptSvc & net stop bits & net stop msiserver & "
            "ren %SystemRoot%\\SoftwareDistribution SoftwareDistribution.old & "
            "ren %SystemRoot%\\System32\\catroot2 catroot2.old & "
            "net start cryptSvc & net start bits & net start msiserver & net start wuauserv"),
        "Start a Windows Update scan": "UsoClient StartScan",
        "Show installed updates": _pscmd(
            r"Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 15 HotFixID,Description,InstalledOn | Format-Table -AutoSize"),
    },
    "System files / repair": {
        "SFC scan (repair system files)": "sfc /scannow",
        "DISM quick health check": "DISM /Online /Cleanup-Image /CheckHealth",
        "DISM deep scan": "DISM /Online /Cleanup-Image /ScanHealth",
        "DISM repair Windows image": "DISM /Online /Cleanup-Image /RestoreHealth",
        "DISM clean up old update files": "DISM /Online /Cleanup-Image /StartComponentCleanup",
        "Repair WMI (verify, then salvage)": "winmgmt /verifyrepository & winmgmt /salvagerepository",
        "Rebuild performance counters": "lodctr /r",
        "Create a system restore point": _pscmd(
            r"Enable-ComputerRestore -Drive ($env:SystemDrive + '\') -ErrorAction SilentlyContinue; "
            r"Checkpoint-Computer -Description 'Windows Fixer AI' -RestorePointType MODIFY_SETTINGS"),
        "Check disk (read-only scan)": "chkdsk %SystemDrive%",
        "Repair system disk at next restart (chkdsk /f)": "echo Y| chkdsk %SystemDrive% /f",
    },
    "Slow PC / Disk space": {
        "Clear user temp files": 'del /q /f /s "%TEMP%\\*" 2>nul',
        "Clear Windows temp files": 'del /q /f /s "%SystemRoot%\\Temp\\*" 2>nul',
        "Empty Recycle Bin": _pscmd(r"Clear-RecycleBin -Force -ErrorAction SilentlyContinue"),
        "Clear Delivery Optimization cache": _pscmd(r"Delete-DeliveryOptimizationCache -Force"),
        "Re-trim SSD": "defrag %SystemDrive% /L",
        "Show free disk space": _pscmd(
            r"Get-Volume | Where-Object {$_.DriveLetter} | Select-Object DriveLetter,FileSystem,HealthStatus,"
            r"@{n='FreeGB';e={[math]::Round($_.SizeRemaining/1GB,1)}},@{n='SizeGB';e={[int]($_.Size/1GB)}} | Format-Table -AutoSize"),
        "Show top memory-using programs": _pscmd(
            r"Get-Process | Sort-Object WS -Descending | Select-Object -First 15 Name,Id,@{n='MB';e={[int]($_.WS/1MB)}} | Format-Table -AutoSize"),
        "Show startup programs": _pscmd(
            r"Get-CimInstance Win32_StartupCommand | Select-Object Name,Command,Location | Format-List"),
        "Show power plans": "powercfg /list",
        "Battery report (laptops)": 'powercfg /batteryreport /output "%USERPROFILE%\\battery-report.html"',
    },
    "Explorer / Start menu / Apps": {
        "Restart Explorer (frozen taskbar)": "taskkill /f /im explorer.exe & start explorer.exe",
        "Rebuild icon cache": (
            'taskkill /f /im explorer.exe & del /a /q "%localappdata%\\IconCache.db" 2>nul & '
            'del /a /f /q "%localappdata%\\Microsoft\\Windows\\Explorer\\iconcache*" 2>nul & start explorer.exe'),
        "Restart Windows Search service": "net stop wsearch /y & net start wsearch",
        "Re-register Start menu and built-in apps": _pscmd(
            r"Get-AppxPackage -AllUsers | ForEach-Object { Add-AppxPackage -DisableDevelopmentMode -Register "
            r"($_.InstallLocation + '\AppXManifest.xml') -ErrorAction SilentlyContinue }"),
        "Re-register Microsoft Store": _pscmd(
            r"Get-AppxPackage -AllUsers Microsoft.WindowsStore | ForEach-Object { Add-AppxPackage -DisableDevelopmentMode "
            r"-Register ($_.InstallLocation + '\AppXManifest.xml') }"),
        "Reset Microsoft Store cache": "wsreset.exe",
        "Clear clipboard": "echo off | clip",
    },
    "Drivers / Devices / Audio / Printer": {
        "Restart audio services": "net stop Audiosrv /y & net stop AudioEndpointBuilder /y & net start AudioEndpointBuilder & net start Audiosrv",
        "Scan for hardware changes": "pnputil /scan-devices",
        "List devices with driver problems": _pscmd(
            r"Get-PnpDevice -PresentOnly | Where-Object {$_.Status -ne 'OK'} | Select-Object Status,Class,FriendlyName,InstanceId | Format-List"),
        "Show graphics driver info": _pscmd(
            r"Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,DriverDate,Status | Format-List"),
        "Restart Bluetooth service": "net stop bthserv & net start bthserv",
        "Restart Print Spooler": "net stop spooler & net start spooler",
        "Clear stuck print queue": 'net stop spooler & del /q /f "%SystemRoot%\\System32\\spool\\PRINTERS\\*" & net start spooler',
    },
    "Security / Clock / Services": {
        "Update Defender signatures": _pscmd(r"Update-MpSignature"),
        "Defender quick scan": _pscmd(r"Start-MpScan -ScanType QuickScan"),
        "Show Defender status": _pscmd(
            r"Get-MpComputerStatus | Select-Object AMServiceEnabled,AntivirusEnabled,RealTimeProtectionEnabled,"
            r"AntivirusSignatureLastUpdated,QuickScanEndTime | Format-List"),
        "Show firewall status": "netsh advfirewall show allprofiles state",
        "Sync clock with internet time": "net start w32time & w32tm /resync /force",
        "Show stopped automatic services": _pscmd(
            r"Get-Service | Where-Object {$_.StartType -eq 'Automatic' -and $_.Status -ne 'Running'} | "
            r"Select-Object Name,DisplayName,Status | Format-Table -AutoSize"),
        "Show startup/boot health (system info)": 'systeminfo | findstr /B /C:"OS" /C:"System Boot Time" /C:"Available Physical Memory"',
    },
}
QUICK_FIXES = {name: cmd for grp in QUICK_FIX_GROUPS.values() for name, cmd in grp.items()}


ENC = "oem" if os.name == "nt" else "utf-8"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SYS_DRIVE = os.environ.get("SystemDrive", "C:")


def _ps(script):
    return ["powershell", "-NoProfile", "-Command", script]


# (label, argv, needs_admin) -- all checks are READ-ONLY
DISK_CHECKS = [
    ("Physical disk health",
     _ps("Get-PhysicalDisk @@D@@| Select FriendlyName,MediaType,BusType,HealthStatus,OperationalStatus,"
         "@{n='SizeGB';e={[int]($_.Size/1GB)}} | Format-List")),
    ("Disk reliability counters (temp, wear, errors, hours)",
     _ps("Get-PhysicalDisk @@D@@| Get-StorageReliabilityCounter | Select DeviceId,Temperature,Wear,"
         "ReadErrorsTotal,WriteErrorsTotal,PowerOnHours | Format-List")),
    ("SMART failure prediction",
     _ps(r"Get-CimInstance -Namespace root\wmi -ClassName MSStorageDriver_FailurePredictStatus "
         r"-ErrorAction SilentlyContinue | Select InstanceName,PredictFailure,Reason | Format-List"), True),
    ("Volumes and free space",
     _ps("Get-Volume @@V@@| Select DriveLetter,FileSystemLabel,FileSystem,HealthStatus,"
         "@{n='FreeGB';e={[int]($_.SizeRemaining/1GB)}},@{n='SizeGB';e={[int]($_.Size/1GB)}} | Format-Table -AutoSize")),
    ("Dirty bit (file system needs repair?)", ["fsutil", "dirty", "query", "@@L@@"], True),
    ("Disk errors in System log (last 30 days)",
     _ps("$e = Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='disk','Ntfs','stornvme',"
         "'storahci','volmgr';Level=1,2,3;StartTime=(Get-Date).AddDays(-30)} -MaxEvents 25 "
         "-ErrorAction SilentlyContinue; if($e){$e | Select TimeCreated,ProviderName,Id,Message | Format-List}"
         "else{'No disk-related errors in the last 30 days'}")),
    ("CHKDSK read-only scan (can take several minutes)", ["chkdsk", "@@L@@"], True),
]
DISK_CHECKS = [c if len(c) == 3 else (c[0], c[1], False) for c in DISK_CHECKS]

# Extra READ-ONLY checks for the "Automatic Repair / startup repair loop" problem
BOOT_CHECKS = [
    ("Startup Repair log (SrtTrail.txt) - says WHY startup repair failed",
     _ps(r"$f=Join-Path $env:SystemRoot 'System32\Logfiles\Srt\SrtTrail.txt'; if(Test-Path $f){Get-Content $f -Raw}"
         r"else{'No SrtTrail.txt (no recent Startup Repair failure recorded)'}")),
    ("Boot configuration (BCD)", ["bcdedit", "/enum"], True),
    ("Windows Recovery Environment (WinRE) status", ["reagentc", "/info"], True),
    ("Boot disk / partition layout",
     _ps("Get-Disk | Select Number,PartitionStyle,HealthStatus,OperationalStatus,IsBoot,IsSystem | Format-Table -AutoSize; "
         "Get-Partition | Select DiskNumber,PartitionNumber,DriveLetter,Type,@{n='MB';e={[int]($_.Size/1MB)}} | Format-Table -AutoSize")),
    ("Pending update / servicing operations",
     _ps(r"'pending.xml present: ' + (Test-Path (Join-Path $env:SystemRoot 'WinSxS\pending.xml')); "
         r"'RebootPending flag: ' + (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'); "
         r"'PackagesPending key: ' + (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\PackagesPending')")),
    ("Recently installed updates",
     _ps("Get-HotFix | Sort-Object InstalledOn -Descending | Select -First 8 HotFixID,Description,InstalledOn | Format-Table -AutoSize")),
    ("Last blue-screen / bugcheck events",
     _ps("$e=Get-WinEvent -FilterHashtable @{LogName='System';Id=1001;StartTime=(Get-Date).AddDays(-30)} -MaxEvents 3 "
         "-ErrorAction SilentlyContinue; if($e){$e | Select TimeCreated,Message | Format-List}else{'No bugcheck events in 30 days'}")),
    ("Critical/error events in System log (last 7 days)",
     _ps("$e=Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2;StartTime=(Get-Date).AddDays(-7)} -MaxEvents 25 "
         "-ErrorAction SilentlyContinue; if($e){$e | Select TimeCreated,ProviderName,Id,@{n='Msg';e={$_.Message.Substring(0,[Math]::Min(160,$_.Message.Length))}} | Format-List}else{'None'}")),
]
BOOT_CHECKS = [c if len(c) == 3 else (c[0], c[1], False) for c in BOOT_CHECKS]

# READ-ONLY checks of a Windows installation on ANOTHER drive (@@L@@ = that drive, e.g. E:)
OFFLINE_CHECKS = [
    ("BitLocker status of the drive", ["manage-bde", "-status", "@@L@@"], True),
    ("Windows installation found on the drive",
     _ps(r"$w='@@L@@\Windows'; $k=Join-Path $w 'System32\ntoskrnl.exe'; if(Test-Path $k){'Windows folder: ' + $w; "
         r"'Kernel version: ' + (Get-Item $k).VersionInfo.ProductVersion}else{'NO Windows installation found on @@L@@ "
         r"(wrong drive, or the drive is locked by BitLocker)'}")),
    ("Startup Repair log (SrtTrail.txt) from that drive - says WHY startup repair failed",
     _ps(r"$f=Join-Path '@@L@@\Windows' 'System32\Logfiles\Srt\SrtTrail.txt'; if(Test-Path $f){Get-Content $f -Raw}"
         r"else{'No SrtTrail.txt on this drive'}")),
    ("Pending update operations on that drive",
     _ps(r"$s=Join-Path '@@L@@\Windows' 'WinSxS'; 'pending.xml present: ' + (Test-Path (Join-Path $s 'pending.xml')); "
         r"Get-ChildItem $s -Filter 'pending*' -ErrorAction SilentlyContinue | Select Name,Length,LastWriteTime | Format-Table -AutoSize")),
    ("Servicing log (CBS.log) - last errors",
     _ps(r"$f=Join-Path '@@L@@\Windows' 'Logs\CBS\CBS.log'; if(Test-Path $f){Select-String -Path $f "
         r"-Pattern 'Error|Failed|Corrupt' | Select-Object -Last 25 | ForEach-Object {$_.Line}}else{'No CBS.log'}")),
    ("Blue-screen dumps on that drive",
     _ps(r"$d=Join-Path '@@L@@\Windows' 'Minidump'; if(Test-Path $d){Get-ChildItem $d | Sort-Object LastWriteTime -Descending | "
         r"Select -First 5 Name,Length,LastWriteTime | Format-Table -AutoSize}else{'No Minidump folder'}; "
         r"'MEMORY.DMP present: ' + (Test-Path '@@L@@\Windows\MEMORY.DMP')")),
    ("Critical/error events from THAT Windows's System log",
     _ps(r"$e=Get-WinEvent -FilterHashtable @{Path='@@L@@\Windows\System32\winevt\Logs\System.evtx';Level=1,2} -MaxEvents 25 "
         r"-ErrorAction SilentlyContinue; if($e){$e | Select TimeCreated,ProviderName,Id,@{n='Msg';e={$_.Message.Substring(0,"
         r"[Math]::Min(160,$_.Message.Length))}} | Format-List}else{'No events readable (log missing or empty)'}")),
    ("Boot configuration (BCD) on that drive - legacy BIOS disks only", ["bcdedit", "/store", "@@L@@\\Boot\\BCD", "/enum"], True),
]
OFFLINE_CHECKS = [c if len(c) == 3 else (c[0], c[1], False) for c in OFFLINE_CHECKS]

# READ-ONLY checks of the WHOLE running PC for "Scan whole PC" / "Scan + auto-fix everything".
# (label, argv, needs_admin).  Everything is wrapped in Out-String so the output order stays correct.
FULL_CHECKS = [
    ("System overview",
     _ps(r"$o=Get-CimInstance Win32_OperatingSystem; $c=Get-CimInstance Win32_ComputerSystem; "
         r"'OS: ' + $o.Caption + ' ' + $o.Version + ' build ' + $o.BuildNumber; "
         r"'PC: ' + $c.Manufacturer + ' ' + $c.Model; 'Last boot: ' + $o.LastBootUpTime; "
         r"'Uptime days: ' + [math]::Round(((Get-Date)-$o.LastBootUpTime).TotalDays,1); "
         r"'RAM free / total GB: ' + [math]::Round($o.FreePhysicalMemory/1MB,1) + ' / ' + [math]::Round($o.TotalVisibleMemorySize/1MB,1)")),
    ("CPU load and busiest programs",
     _ps(r"'CPU load %: ' + (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average; "
         r"'Top programs by memory:'; (Get-Process | Sort-Object WS -Descending | Select-Object -First 8 Name,Id,"
         r"@{n='MB';e={[int]($_.WS/1MB)}} | Format-Table -AutoSize | Out-String); "
         r"'Top programs by CPU time:'; (Get-Process | Sort-Object CPU -Descending | Select-Object -First 6 Name,Id,"
         r"@{n='CPUsec';e={[int]$_.CPU}} | Format-Table -AutoSize | Out-String)")),
    ("Disk space and disk health",
     _ps(r"(Get-Volume | Where-Object {$_.DriveType -eq 'Fixed' -and $_.DriveLetter} | Select-Object DriveLetter,FileSystem,"
         r"HealthStatus,@{n='FreeGB';e={[math]::Round($_.SizeRemaining/1GB,1)}},@{n='SizeGB';e={[int]($_.Size/1GB)}} | "
         r"Format-Table -AutoSize | Out-String); (Get-PhysicalDisk | Select-Object FriendlyName,MediaType,HealthStatus,"
         r"OperationalStatus | Format-Table -AutoSize | Out-String)")),
    ("Temp files size",
     _ps(r"foreach($p in @($env:TEMP,(Join-Path $env:SystemRoot 'Temp'))){ $m=Get-ChildItem $p -Recurse -Force "
         r"-ErrorAction SilentlyContinue | Measure-Object Length -Sum; $p + ': ' + [int]($m.Sum/1MB) + ' MB in ' + $m.Count + ' files' }")),
    ("Network adapters and internet connectivity",
     _ps(r"(Get-NetAdapter | Select-Object Name,Status,LinkSpeed | Format-Table -AutoSize | Out-String); "
         r"(Get-NetIPConfiguration | Select-Object InterfaceAlias,@{n='IPv4';e={$_.IPv4Address.IPAddress}},"
         r"@{n='Gateway';e={$_.IPv4DefaultGateway.NextHop}},@{n='DNS';e={$_.DNSServer.ServerAddresses -join ','}} | "
         r"Format-Table -AutoSize | Out-String); "
         r"'Ping 8.8.8.8 (internet by IP): ' + (Test-Connection 8.8.8.8 -Count 2 -Quiet -ErrorAction SilentlyContinue); "
         r"'DNS lookup of microsoft.com works: ' + [bool](Resolve-DnsName microsoft.com -ErrorAction SilentlyContinue)")),
    ("Proxy and hosts file (can silently break the internet)",
     _ps(r"'WinHTTP proxy:'; (netsh winhttp show proxy | Out-String); 'User proxy enabled: ' + "
         r"(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue).ProxyEnable; "
         r"'Hosts file entries (normally none):'; Get-Content (Join-Path $env:SystemRoot 'System32\drivers\etc\hosts') | "
         r"Where-Object { $_.Trim() -and $_ -notmatch '^\s*#' }")),
    ("Key Windows services",
     _ps(r"(Get-Service -Name wuauserv,bits,cryptsvc,TrustedInstaller,Audiosrv,AudioEndpointBuilder,spooler,bthserv,WlanSvc,"
         r"Dhcp,Dnscache,EventLog,WinDefend,wscsvc,Winmgmt,W32Time,wsearch,LanmanWorkstation,mpssvc,BFE -ErrorAction SilentlyContinue | "
         r"Select-Object Name,Status,StartType | Format-Table -AutoSize | Out-String)")),
    ("Automatic services that are not running",
     _ps(r"$s=Get-Service | Where-Object {$_.StartType -eq 'Automatic' -and $_.Status -ne 'Running'} | Select-Object -First 30 "
         r"Name,DisplayName,Status; if($s){$s | Format-Table -AutoSize | Out-String}else{'All automatic services are running'}; "
         r"'(a few are normal: update checkers and other trigger-start services)'")),
    ("Windows Update health",
     _ps(r"'Update errors in the last 14 days:'; $e=Get-WinEvent -FilterHashtable @{LogName='System';"
         r"ProviderName='Microsoft-Windows-WindowsUpdateClient';Level=2,3;StartTime=(Get-Date).AddDays(-14)} -MaxEvents 8 "
         r"-ErrorAction SilentlyContinue; if($e){$e | ForEach-Object { $_.TimeCreated.ToString() + ' id ' + $_.Id + ': ' + "
         r"$_.Message.Substring(0,[Math]::Min(180,$_.Message.Length)) }}else{'none'}; 'Latest installed updates:'; "
         r"(Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 5 HotFixID,InstalledOn | Format-Table -AutoSize | Out-String); "
         r"'SoftwareDistribution size MB: ' + [int]((Get-ChildItem (Join-Path $env:SystemRoot 'SoftwareDistribution') -Recurse -Force "
         r"-ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum/1MB)")),
    ("Pending restart / half-installed updates",
     _ps(r"$b='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion'; "
         r"'CBS RebootPending: ' + (Test-Path ($b+'\Component Based Servicing\RebootPending')); "
         r"'CBS PackagesPending: ' + (Test-Path ($b+'\Component Based Servicing\PackagesPending')); "
         r"'WindowsUpdate RebootRequired: ' + (Test-Path ($b+'\WindowsUpdate\Auto Update\RebootRequired')); "
         r"'PendingFileRenameOperations: ' + [bool](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' "
         r"-Name PendingFileRenameOperations -ErrorAction SilentlyContinue); "
         r"'pending.xml present: ' + (Test-Path (Join-Path $env:SystemRoot 'WinSxS\pending.xml'))")),
    ("Devices with driver problems",
     _ps(r"$d=Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object {$_.Status -ne 'OK'}; "
         r"if($d){$d | Select-Object Status,Class,FriendlyName,InstanceId | Format-List | Out-String}else{'No devices with driver problems'}")),
    ("Graphics and audio devices",
     _ps(r"(Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,DriverDate,Status | Format-List | Out-String); "
         r"(Get-CimInstance Win32_SoundDevice | Select-Object Name,Status | Format-Table -AutoSize | Out-String)")),
    ("Most frequent errors in the event logs (last 3 days)",
     _ps(r"$e=Get-WinEvent -FilterHashtable @{LogName='System','Application';Level=1,2;StartTime=(Get-Date).AddDays(-3)} "
         r"-MaxEvents 400 -ErrorAction SilentlyContinue; if($e){$e | Group-Object ProviderName,Id | Sort-Object Count -Descending | "
         r"Select-Object -First 12 Count,@{n='Source,Id';e={$_.Name}},@{n='Sample';e={(($_.Group[0].Message + '').Substring(0,"
         r"[Math]::Min(120,($_.Group[0].Message + '').Length))) -replace '\s+',' '}} | Format-List | Out-String}"
         r"else{'No critical/error events in the last 3 days'}")),
    ("Blue screens, crashes and hardware errors (30 days)",
     _ps(r"$s=(Get-Date).AddDays(-30); $k=@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Kernel-Power';"
         r"Id=41;StartTime=$s} -ErrorAction SilentlyContinue); 'Unexpected shutdowns (Kernel-Power 41): ' + $k.Count; "
         r"$b=@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-WER-SystemErrorReporting';Id=1001;"
         r"StartTime=$s} -ErrorAction SilentlyContinue); 'Blue screens: ' + $b.Count; $b | Select-Object -First 3 | ForEach-Object "
         r"{ $_.TimeCreated.ToString() + ' ' + $_.Message.Substring(0,[Math]::Min(220,$_.Message.Length)) }; "
         r"$w=@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-WHEA-Logger';StartTime=$s} "
         r"-ErrorAction SilentlyContinue); 'Hardware errors (WHEA): ' + $w.Count; "
         r"'Minidump files: ' + @(Get-ChildItem (Join-Path $env:SystemRoot 'Minidump') -ErrorAction SilentlyContinue).Count")),
    ("Programs that crashed most (14 days)",
     _ps(r"$e=@(Get-WinEvent -FilterHashtable @{LogName='Application';ProviderName='Application Error';Id=1000;"
         r"StartTime=(Get-Date).AddDays(-14)} -MaxEvents 100 -ErrorAction SilentlyContinue); if($e.Count){$e | ForEach-Object "
         r"{ $_.Properties[0].Value } | Group-Object | Sort-Object Count -Descending | Select-Object -First 8 Count,Name | "
         r"Format-Table -AutoSize | Out-String}else{'No application crashes in the last 14 days'}")),
    ("Built-in apps health (Store, Start menu, shell)",
     _ps(r"(Get-AppxPackage | Where-Object { $_.Name -in 'Microsoft.WindowsStore','Microsoft.Windows.ShellExperienceHost',"
         r"'Microsoft.Windows.StartMenuExperienceHost' } | Select-Object Name,Version,Status | Format-Table -AutoSize | Out-String)")),
    ("Windows Defender and firewall",
     _ps(r"try{$m=Get-MpComputerStatus -ErrorAction Stop; ($m | Select-Object AMServiceEnabled,AntivirusEnabled,"
         r"RealTimeProtectionEnabled,AntivirusSignatureLastUpdated,AntivirusSignatureAge,QuickScanEndTime | Format-List | Out-String)}"
         r"catch{'Defender status unavailable (another antivirus may be active): ' + $_.Exception.Message}; "
         r"$t=@(Get-MpThreatDetection -ErrorAction SilentlyContinue | Select-Object -First 5); 'Recent threat detections: ' + $t.Count; "
         r"(Get-NetFirewallProfile -ErrorAction SilentlyContinue | Select-Object Name,Enabled | Format-Table -AutoSize | Out-String)")),
    ("Clock and time zone",
     _ps(r"'Time zone: ' + (Get-TimeZone).Id; 'Now: ' + (Get-Date); (w32tm /query /status 2>&1 | Out-String)")),
    ("Startup programs",
     _ps(r"$s=@(Get-CimInstance Win32_StartupCommand); 'Startup programs: ' + $s.Count; "
         r"($s | Select-Object -First 25 Name,Command | Format-Table -AutoSize -Wrap | Out-String)")),
    ("Power plan and battery",
     _ps(r"(powercfg /getactivescheme | Out-String); $b=Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue; "
         r"if($b){'Battery charge %: ' + $b.EstimatedChargeRemaining + ' status ' + $b.BatteryStatus}else{'No battery (desktop PC)'}")),
    ("Printers",
     _ps(r"$p=@(Get-Printer -ErrorAction SilentlyContinue); if($p.Count){($p | Select-Object Name,PrinterStatus,JobCount | "
         r"Format-Table -AutoSize | Out-String)}else{'No printers installed'}")),
    ("Windows activation",
     _ps(r"Get-CimInstance SoftwareLicensingProduct -Filter 'PartialProductKey IS NOT NULL' | Where-Object { $_.Name -like 'Windows*' } | "
         r"Select-Object Name,@{n='Licensed';e={$_.LicenseStatus -eq 1}} | Format-List | Out-String")),
    ("System restore points",
     _ps(r"$r=Get-ComputerRestorePoint -ErrorAction SilentlyContinue | Select-Object -Last 3 CreationTime,Description; "
         r"if($r){$r | Format-Table -AutoSize | Out-String}else{'No restore points (System Protection may be off)'}"), True),
    ("Windows image health (DISM CheckHealth)", ["DISM", "/Online", "/Cleanup-Image", "/CheckHealth"], True),
    ("WMI repository", ["winmgmt", "/verifyrepository"], True),
]
FULL_CHECKS = [c if len(c) == 3 else (c[0], c[1], False) for c in FULL_CHECKS]

BOOT_RULES = (
    "\n\n[BOOT / AUTOMATIC REPAIR LOOP] The PC keeps entering 'Preparing Automatic Repair' / startup repair loops, but Windows is "
    "running right now. Use the diagnostics (SrtTrail.txt, pending operations, disk health, bugcheck/event log, BCD, WinRE status, "
    "recent updates) to find the most likely cause, then fix it:\n"
    "- Failed/pending update -> DISM /Online /Cleanup-Image /RevertPendingActions, then rename (not delete) "
    "%SystemRoot%\\WinSxS\\pending.xml to pending.xml.bak if it exists.\n"
    "- Corrupted system files -> DISM /Online /Cleanup-Image /RestoreHealth, then sfc /scannow.\n"
    "- File-system errors -> chkdsk on the system drive (see drive rules; it runs at next restart).\n"
    "- WinRE disabled/broken -> reagentc /enable.\n"
    "- First back up the BCD: bcdedit /export \"%SystemDrive%\\bcd_backup\". To stop the loop: "
    "bcdedit /set {default} recoveryenabled No and bcdedit /set {default} bootstatuspolicy ignoreallfailures "
    "(tell the user to undo with recoveryenabled Yes later).\n"
    "- ONLY these bcdedit forms are allowed: /enum, /export <file>, /set {default|current} recoveryenabled|bootstatuspolicy|"
    "bootmenupolicy <value>. Everything else on bcdedit is blocked.\n"
    "- Most boot fixes need a restart: finish with NEED_USER: restart the PC and tell me if the loop continues.\n"
    "- If logs point to a bad driver, name it and explain how the user can remove it; do not delete files. "
    "If the disk is failing, say so and tell the user to back up."
)
BOOT_OK = re.compile(
    r"^bcdedit(?:\.exe)?\s+(?:/enum(?:\s+(?:all|\{\w+\}))?"
    r"|/export\s+\"?[\w:\\%.\- ]+\"?"
    r"|/set\s+\{(?:default|current)\}\s+(?:recoveryenabled|bootstatuspolicy|bootmenupolicy)\s+\w+)\s*$", re.I)


# ---------- FULL PC SCAN & FIX ----------
FULL_MAX_ROUNDS = 10
RESTORE_PS = (r"Enable-ComputerRestore -Drive ($env:SystemDrive + '\') -ErrorAction SilentlyContinue; "
              r"Checkpoint-Computer -Description 'Windows Fixer AI (before auto-fix)' -RestorePointType MODIFY_SETTINGS")

# Temp-file cleanup is the one kind of delete that auto-fix may run (needed to fix 'disk full' / 'slow PC').
SAFE_CLEAN = re.compile(
    r'^del(?:\s+/[qfs])+\s+"?(?:%TEMP%|%TMP%|%LOCALAPPDATA%\\Temp|%SystemRoot%\\Temp|%WINDIR%\\Temp)\\\*"?(?:\s+2>nul)?\s*$',
    re.I)

FULL_RULES = (
    "\n\n[FULL PC REPAIR] The first message is a read-only scan of the WHOLE PC (system, disk, network, updates, services, "
    "drivers, errors, security, performance). Your job: fix every REAL problem you can see.\n"
    "- First reply: a short numbered list of the real problems, most serious first (ignore harmless noise such as a few old "
    "event-log errors or services that start on demand), then start fixing the first group with ONE ```cmd block (max 5 commands).\n"
    "- Work one problem group per round, in this order: corrupted system files / pending updates, disk errors or low space, "
    "stopped essential services, network, Windows Update, drivers/devices, crashing apps, performance/startup, security "
    "(update Defender signatures + quick scan; never turn protection off).\n"
    "- After each fix, re-run a read-only check to verify it really worked before moving on.\n"
    "- Rename, never delete (for example ren SoftwareDistribution SoftwareDistribution.old). The ONLY deletes allowed are "
    "temp-file cleanups: del /q /f /s \"%TEMP%\\*\" 2>nul  and the same for \"%SystemRoot%\\Temp\\*\".\n"
    "- Things that cannot be fixed automatically (failing disk, a driver that needs the maker's download, BIOS update, "
    "reinstalling an app, blue-screen dump analysis, anything that needs a restart) go into the final NEED_USER list "
    "with exact, numbered steps.\n"
    "- Finish with a summary: FIXED: ...  STILL OPEN: ...  and then FIX_DONE or NEED_USER: ..."
)

TOOLBOX = r'''

[TOOLBOX - known-safe fixes by problem type; prefer these over improvising]
- No internet / DNS / Wi-Fi: ipconfig /flushdns ; ipconfig /release then ipconfig /renew ; netsh winhttp reset proxy ; net stop wlansvc /y then net start wlansvc ; netsh winsock reset and netsh int ip reset (finish after a restart -> NEED_USER) ; powershell -NoProfile -Command "Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | Restart-NetAdapter -Confirm:$false"
- Windows Update stuck/failing: net stop wuauserv ; net stop bits ; net stop cryptSvc ; ren %SystemRoot%\SoftwareDistribution SoftwareDistribution.old ; ren %SystemRoot%\System32\catroot2 catroot2.old ; net start cryptSvc ; net start bits ; net start wuauserv ; UsoClient StartScan ; DISM /Online /Cleanup-Image /RestoreHealth
- Corrupt files / apps crash or will not open / Settings or Start broken: DISM /Online /Cleanup-Image /CheckHealth then /RestoreHealth ; sfc /scannow ; powershell -NoProfile -Command "Get-AppxPackage -AllUsers | ForEach-Object { Add-AppxPackage -DisableDevelopmentMode -Register ($_.InstallLocation + '\AppXManifest.xml') -ErrorAction SilentlyContinue }" ; wsreset.exe
- Slow PC / disk almost full: list top processes and startup programs first; del /q /f /s "%TEMP%\*" 2>nul ; DISM /Online /Cleanup-Image /StartComponentCleanup ; powershell -NoProfile -Command "Clear-RecycleBin -Force -ErrorAction SilentlyContinue" ; powershell -NoProfile -Command "Delete-DeliveryOptimizationCache -Force" ; defrag %SystemDrive% /L (SSD re-trim). Tell the user which startup programs to disable in Task Manager (do not edit the registry).
- Essential service stopped: net start <service> ; sc config <service> start= auto (Microsoft Windows services only, never third-party).
- No sound: net stop Audiosrv /y ; net stop AudioEndpointBuilder /y ; net start AudioEndpointBuilder ; net start Audiosrv ; list sound devices.
- Devices / drivers with problems: powershell Get-PnpDevice -PresentOnly | Where-Object {$_.Status -ne 'OK'} ; pnputil /scan-devices ; if a vendor driver is needed -> NEED_USER with the device name.
- Printer: net stop spooler ; del /q /f "%SystemRoot%\System32\spool\PRINTERS\*" ; net start spooler.
- Bluetooth: net stop bthserv ; net start bthserv.
- Clock wrong: net start w32time ; w32tm /resync /force.
- Search / taskbar / Explorer problems: net stop wsearch /y ; net start wsearch ; taskkill /f /im explorer.exe then start explorer.exe.
- WMI errors: winmgmt /verifyrepository then winmgmt /salvagerepository.
- Security: powershell Update-MpSignature ; powershell Start-MpScan -ScanType QuickScan. Never disable Defender or the firewall.
- Blue screens / unexpected restarts: read the bugcheck + Kernel-Power events, run DISM + sfc, check disk health; memory test (mdsched), driver rollback and BIOS updates -> NEED_USER.
- Before changing system settings you may create a restore point: powershell -NoProfile -Command "Checkpoint-Computer -Description 'Windows Fixer AI' -RestorePointType MODIFY_SETTINGS"
'''


def cmd_blocked(c, boot=False, offline=False):
    """True if auto-fix must not run this command."""
    if offline and _OFFLINE_BCD.match(c.strip()):   # bcdedit /store X:\\Boot\\BCD ... (offline drive only)
        return False
    if boot and not offline and BOOT_OK.match(c.strip()):
        return False
    if not offline and SAFE_CLEAN.match(c.strip()):   # temp-file cleanup is the one delete auto-fix may run
        return False
    return any(re.search(p, c, re.I) for p in AUTO_BLOCK)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
THINK_RE = re.compile(r"<think>.*?</think>", re.S)  # reasoning models (e.g. DeepSeek R1) wrap thoughts in these

KNOWN_FREE = {"big-pickle"}


def is_free_name(mid):
    return mid.endswith("-free") or mid in KNOWN_FREE


def modelsdev_free():
    """Best-effort: ids of OpenCode models that cost 0 in the public models.dev catalog (no API key sent)."""
    try:
        req = urllib.request.Request("https://models.dev/api.json", headers={"User-Agent": "WindowsFixerAI/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        out = set()
        for mid, m in data.get("opencode", {}).get("models", {}).items():
            c = m.get("cost") or {}
            if c.get("input") == 0 and c.get("output") == 0:
                out.add(m.get("id", mid))
        return out
    except Exception:
        return set()


LIMIT_HELP = (
    "Free usage limit reached on the shared 'public' key. Options:\n"
    "1) Sign in at https://opencode.ai, create your own API key and paste it into the API key box.\n"
    "2) Pick a different model in the Model list and try again.\n"
    "3) Click 'Install OpenCode on this PC' and tick 'Prefer OpenCode CLI' - free models often still work inside OpenCode itself.\n"
    "4) Wait a while and retry."
)

NV_HELP = (
    "NVIDIA NIM needs your own API key:\n"
    "1) Go to https://build.nvidia.com and sign in (free).\n"
    "2) Open your profile > API Keys and generate a key (starts with nvapi-).\n"
    "3) Paste it into the API key box, click 'Save key', then try again.\n"
    "Free credits are limited; if you hit a rate limit, pick another model or wait."
)

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WindowsFixerAI")
CACHE_FILE = os.path.join(APP_DIR, "models.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def load_settings():
    d = _read_json(SETTINGS_FILE)
    return d if isinstance(d, dict) else {}


def save_settings(d):
    _write_json(SETTINGS_FILE, d)


def env_value(name):
    """Read an environment variable, also from the Windows registry (catches keys set with setx after launch)."""
    v = os.environ.get(name, "").strip()
    if v or os.name != "nt":
        return v
    try:
        import winreg
        for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(root, sub) as k:
                    val, _ = winreg.QueryValueEx(k, name)
                    if val and str(val).strip():
                        return str(val).strip()
            except OSError:
                pass
    except Exception:
        pass
    return ""


def detect_keys():
    """Look for API keys already on this PC. Returns {provider_id: (key, where_found)}."""
    found = {}
    for pid, cfg in PROVIDERS.items():
        for name in cfg["env"]:
            v = env_value(name)
            if v:
                found[pid] = (v, f"environment variable {name}")
                break
    # OpenCode stores logins in auth.json: {"nvidia": {"type": "api", "key": "..."}, ...}
    bases = [os.path.join(os.path.expanduser("~"), ".local", "share", "opencode")]
    for var in ("APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME"):
        if os.environ.get(var):
            bases.append(os.path.join(os.environ[var], "opencode"))
    aliases = {"opencode": ("opencode",), "nvidia": ("nvidia", "nvidia-nim")}
    for base in bases:
        path = os.path.join(base, "auth.json")
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        for pid, names in aliases.items():
            if pid in found:
                continue
            for n in names:
                e = data.get(n)
                if isinstance(e, dict) and str(e.get("key", "")).strip():
                    found[pid] = (str(e["key"]).strip(), f"OpenCode login file {path}")
                    break
    return found


_GPU_CACHE = []


def detect_nvidia_gpu():
    """Return e.g. 'NVIDIA GeForce RTX 4060, driver 551.23, 8188 MiB' or '' if no NVIDIA GPU / driver found."""
    if _GPU_CACHE:
        return _GPU_CACHE[0]
    exe = shutil.which("nvidia-smi")
    if not exe:
        p = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")
        exe = p if os.path.exists(p) else None
    out = ""
    if exe:
        try:
            r = subprocess.run([exe, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
            out = " | ".join(ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip())
        except Exception:
            pass
    _GPU_CACHE.append(out)
    return out


def nvidia_chat_models(ids, dead=(), good=()):
    """Keep text-chat models only. Order: verified-working, then preferred, then the rest. Known-dead are dropped."""
    ids = [i for i in ids if i not in dead and not any(s in i.lower() for s in NV_SKIP)]
    first = [m for m in good if m in ids]
    pref = [m for m in PROVIDERS["nvidia"]["fallback"] if m in ids and m not in first]
    return first + pref + sorted(i for i in ids if i not in first and i not in pref)


def find_opencode():
    p = shutil.which("opencode")
    if p:
        return p
    appdata = os.environ.get("APPDATA")
    if appdata:
        for n in ("opencode.cmd", "opencode.exe", "opencode"):
            c = os.path.join(appdata, "npm", n)
            if os.path.exists(c):
                return c
    return None


def save_cache(pid, ids):
    data = _read_json(CACHE_FILE)
    if isinstance(data, list):  # old single-provider format
        data = {"opencode": data}
    if not isinstance(data, dict):
        data = {}
    data[pid] = ids
    _write_json(CACHE_FILE, data)


def load_cache(pid):
    data = _read_json(CACHE_FILE)
    if isinstance(data, list):
        data = {"opencode": data}
    ids = data.get(pid) if isinstance(data, dict) else None
    return ids if isinstance(ids, list) else []


def list_drives():
    out = []
    if os.name != "nt":
        return out
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if mask & (1 << i):
            letter = chr(65 + i)
            if ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") in (2, 3):  # removable / fixed
                try:
                    t, u, f = shutil.disk_usage(f"{letter}:\\")
                    out.append(f"{letter}:  ({t // 2**30} GB, {f // 2**30} GB free)")
                except Exception:
                    out.append(f"{letter}:  (not ready)")
    return out


def has_windows(L):
    """True if drive L: holds a Windows installation (kernel file is readable without Administrator)."""
    return os.path.isfile(f"{L}:\\Windows\\System32\\ntoskrnl.exe")


def _quick(argv, timeout=25):
    """Run one short read-only command, return its output text."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding=ENC, errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return f"[error] {e}"


def detect_os(L):
    """What is on drive L:?  -> (label, kind) with kind = 'windows', 'other' or 'none'."""
    if has_windows(L):
        ver = _quick(_ps(f"(Get-Item '{L}:\\Windows\\System32\\ntoskrnl.exe').VersionInfo.ProductVersion"), 20)
        m = re.match(r"(\d+)\.(\d+)\.(\d+)", ver)
        if m:
            major, minor, build = (int(x) for x in m.groups())
            name = ("Windows 11" if build >= 22000 else "Windows 10") if major == 10 else f"Windows {major}.{minor}"
            return f"{name} (build {build})", "windows"
        return "Windows (version unreadable)", "windows"
    if os.path.isfile(f"{L}:\\etc\\os-release"):
        return "Linux (system files found)", "other"
    if os.path.isdir(f"{L}:\\System\\Library\\CoreServices"):
        return "macOS (system files found)", "other"
    if _quick(_ps(f"(Get-Volume -DriveLetter {L}).FileSystem"), 20).upper() == "RAW":
        return "unknown - RAW file system (maybe another OS)", "other"
    return "no operating system found", "none"


def quick_tests(L, admin):
    """Fast READ-ONLY tests of the OS on drive L:  ->  (os_label, [(test, STATUS, detail), ...]).
    STATUS is OK / WARN / FAIL / SKIP.  Works for the running Windows and for another drive (offline)."""
    on_sys = (L + ":").upper() == SYS_DRIVE.upper()
    label, kind = detect_os(L)
    rows = []

    # BitLocker first: a locked drive hides its OS
    locked = False
    if not on_sys:
        if not admin:
            bl = ("BitLocker", "SKIP", "needs Administrator")
        else:
            out = _quick(["manage-bde", "-status", f"{L}:"], 25).lower()
            m = re.search(r"lock status:\s*(\w+)", out)
            if m and m.group(1) == "locked":
                locked = True
                bl = ("BitLocker", "FAIL", f"drive is locked - run: manage-bde -unlock {L}: -RecoveryPassword <key>")
            elif m or "fully decrypted" in out or "protection off" in out:
                bl = ("BitLocker", "OK", "not locked")
            else:
                bl = ("BitLocker", "SKIP", "status unreadable")
        rows.append(bl)

    if kind == "windows":
        rows.insert(0, ("Operating system", "OK", label + (" - running now" if on_sys else f" - offline on {L}:")))
    elif kind == "other":
        rows.insert(0, ("Operating system", "WARN", label + " - this app can only repair Windows"))
    else:
        rows.insert(0, ("Operating system", "FAIL", "locked by BitLocker" if locked else label))

    # file system dirty bit
    if not admin:
        rows.append(("File system", "SKIP", "needs Administrator"))
    else:
        out = _quick(["fsutil", "dirty", "query", f"{L}:"], 20)
        low = out.lower()
        if "not dirty" in low:
            rows.append(("File system", "OK", "clean (dirty bit not set)"))
        elif "dirty" in low:
            rows.append(("File system", "FAIL", "dirty - needs chkdsk" + (" (runs at next restart)" if on_sys else "")))
        else:
            rows.append(("File system", "WARN", ((out.splitlines() or ["unreadable"])[0])[:80]))

    # physical disk health
    lines = _quick(_ps(f"(Get-Partition -DriveLetter {L} | Get-Disk).HealthStatus"), 25).splitlines()
    h = lines[0].strip() if lines else ""
    st = {"healthy": "OK", "warning": "WARN", "unhealthy": "FAIL"}.get(h.lower())
    rows.append(("Disk health", st or "SKIP", h if st else "unknown (" + (h[:60] or "no data") + ")"))

    if kind != "windows":
        rows.append(("Windows checks", "SKIP", "not a Windows installation"))
        return label, rows

    # half-installed updates
    if os.path.isfile(f"{L}:\\Windows\\WinSxS\\pending.xml"):
        rows.append(("Pending updates", "WARN", "pending.xml present (half-installed update - common loop cause)"))
    else:
        flag = False
        if on_sys:
            out = _quick(_ps(r"$b='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing'; "
                             r"(Test-Path ($b+'\RebootPending')) -or (Test-Path ($b+'\PackagesPending'))"), 20)
            flag = out.strip().lower() == "true"
        rows.append(("Pending updates", "WARN" if flag else "OK", "update restart/packages pending" if flag else "none"))

    # earlier startup-repair failure
    srt = f"{L}:\\Windows\\System32\\Logfiles\\Srt\\SrtTrail.txt"
    if os.path.isfile(srt):
        try:
            when = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(srt)))
        except Exception:
            when = "unknown date"
        rows.append(("Startup Repair log", "WARN", f"SrtTrail.txt from {when} - startup repair failed before"))
    else:
        rows.append(("Startup Repair log", "OK", "no startup-repair failure logged"))

    # boot setup
    if on_sys:
        if not admin:
            rows.append(("Boot setup", "SKIP", "needs Administrator"))
        else:
            out = _quick(["reagentc", "/info"], 20).lower()
            if "disabled" in out:
                rows.append(("Boot setup", "WARN", "Windows Recovery Environment disabled (fix: reagentc /enable)"))
            elif "enabled" in out:
                rows.append(("Boot setup", "OK", "Windows Recovery Environment enabled"))
            else:
                rows.append(("Boot setup", "SKIP", "WinRE status unreadable"))
    elif os.path.isfile(f"{L}:\\Boot\\BCD"):
        rows.append(("Boot setup", "OK", "BCD found on the drive (legacy BIOS disk)"))
    else:
        rows.append(("Boot setup", "SKIP", "UEFI: boot files are on a hidden EFI partition (not testable here)"))
    return label, rows


# ---------- virtual machine (QEMU) helpers ----------
QEMU_URL = "https://www.qemu.org/download/#windows"


def find_qemu(saved=""):
    cands = [saved] if saved else []
    p = shutil.which("qemu-system-x86_64")
    if p:
        cands.append(p)
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            cands.append(os.path.join(base, "qemu", "qemu-system-x86_64.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def find_firmware(qemu):
    """(UEFI code file, UEFI variable-store template) from QEMU's share folder - None when missing."""
    share = os.path.join(os.path.dirname(qemu), "share")
    code = os.path.join(share, "edk2-x86_64-code.fd")
    nv = os.path.join(share, "edk2-i386-vars.fd")
    return (code if os.path.isfile(code) else None), (nv if os.path.isfile(nv) else None)


def qmp_cmd(sock, rf, name, args=None):
    """One QEMU monitor (QMP) command; returns its result."""
    msg = {"execute": name}
    if args:
        msg["arguments"] = args
    sock.sendall((json.dumps(msg) + "\n").encode())
    while True:
        line = rf.readline()
        if not line:
            raise ConnectionError("QEMU closed the monitor connection")
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if "return" in d:
            return d["return"]
        if "error" in d:
            raise RuntimeError(d["error"].get("desc", "QMP error"))


def vm_shutdown(proc, sock, rf):
    try:
        if proc.poll() is None and sock:
            try:
                qmp_cmd(sock, rf, "quit")
            except Exception:
                pass
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        if sock:
            sock.close()
    except Exception:
        pass


def ppm_parse(data):
    """(width, height, pixel offset) of a binary PPM (P6) image, or None."""
    pos, toks, n = 0, [], len(data)
    while len(toks) < 4 and pos < n:
        while pos < n and data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            while pos < n and data[pos:pos + 1] != b"\n":
                pos += 1
            continue
        start = pos
        while pos < n and not data[pos:pos + 1].isspace():
            pos += 1
        toks.append(data[start:pos])
    if len(toks) < 4 or toks[0] != b"P6":
        return None
    try:
        w, h = int(toks[1]), int(toks[2])
    except ValueError:
        return None
    pos += 1
    if w <= 0 or h <= 0 or n - pos < w * h * 3:
        return None
    return w, h, pos


def ppm_analyze(data, max_w=272):
    """Shrink a PPM screenshot to fit the VM panel and measure it.
    -> (stats, small_ppm_bytes) or None.  stats: nonblack / blue fractions + a coarse signature to spot changes."""
    info = ppm_parse(data)
    if not info:
        return None
    w, h, off = info
    step = max(1, -(-w // max_w))
    rows, sig = [], bytearray()
    total = nonblack = blue = 0
    for y in range(0, h, step):
        base = off + y * w * 3
        parts = []
        for x in range(0, w, step):
            i = base + x * 3
            px = data[i:i + 3]
            parts.append(px)
            r, g, b = px
            total += 1
            if r > 24 or g > 24 or b > 24:
                nonblack += 1
            if b > 100 and b > r + 70 and b >= g:
                blue += 1
            if total % 29 == 0:
                sig.append(((r >> 5) << 6) | ((g >> 5) << 3) | (b >> 5))
        rows.append(b"".join(parts))
    sw, sh = len(range(0, w, step)), len(rows)
    small = b"P6 %d %d 255\n" % (sw, sh) + b"".join(rows)
    return {"w": w, "h": h, "nonblack": nonblack / total, "blue": blue / total, "sig": bytes(sig)}, small


def ppm_changed(a, b):
    """True if two screenshot signatures differ noticeably (a blinking cursor does not count)."""
    if not a or len(a) != len(b):
        return True
    return sum(1 for x, y in zip(a, b) if x != y) / len(a) > 0.02


def ppm_to_bmp(data):
    """Full-size PPM screenshot -> 24-bit BMP bytes (Windows' built-in OCR can open BMP files)."""
    info = ppm_parse(data)
    if not info:
        return None
    w, h, off = info
    rowlen = (w * 3 + 3) & ~3
    pad = b"\x00" * (rowlen - w * 3)
    rows = []
    for y in range(h - 1, -1, -1):          # BMP stores the bottom row first
        base = off + y * w * 3
        r = data[base:base + w * 3:3]
        b = data[base + 2:base + w * 3:3]
        row = bytearray(data[base:base + w * 3])
        row[0::3] = b                       # RGB -> BGR
        row[2::3] = r
        rows.append(bytes(row) + pad)
    pix = b"".join(rows)
    return (b"BM" + struct.pack("<IHHI", 54 + len(pix), 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pix), 2835, 2835, 0, 0) + pix)


# Reads the text on a picture with Windows' built-in OCR (Windows 10/11, Windows PowerShell 5.1, no downloads).
OCR_PS = r'''param([string]$Path)
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics,ContentType=WindowsRuntime]
  $null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics,ContentType=WindowsRuntime]
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, $type) { $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if (-not $engine) { 'OCR_UNAVAILABLE: no OCR language installed'; exit 0 }
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $result.Lines | ForEach-Object { $_.Text }
} catch { 'OCR_UNAVAILABLE: ' + $_.Exception.Message }
'''


def ocr_screens(tmp, shots):
    """OCR the VM screenshots -> [(seconds, text)].  Never raises; text starting with '(' means OCR did not work."""
    out = []
    try:
        ps1 = os.path.join(tmp, "ocr.ps1")
        with open(ps1, "w", encoding="utf-8") as f:
            f.write(OCR_PS)
        seen = set()
        for i, (t, raw) in enumerate(shots):
            bmp = ppm_to_bmp(raw)
            if not bmp:
                continue
            p = os.path.join(tmp, f"shot{i}.bmp")
            with open(p, "wb") as f:
                f.write(bmp)
            txt = _quick(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, p], 90)
            lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            if lines and lines[0].startswith("OCR_UNAVAILABLE"):
                out.append((t, "(OCR not available: " + lines[0][16:].strip()[:120] + ")"))
                break
            text = " / ".join(lines)[:500]
            if text and text not in seen:
                seen.add(text)
                out.append((t, text))
    except Exception as e:
        out.append((0, f"(OCR failed: {e})"))
    return out


def vm_report_text(L, ph, uefi, secs, elapsed, exited, result, timeline, texts):
    """Plain-text description of what the VM showed, for the AI (the AI cannot see pictures)."""
    names = {"black": "black screen", "blue": "BLUE screen", "picture": "picture on screen"}
    segs = []
    for t, kind in timeline:
        if segs and segs[-1][2] == kind:
            segs[-1][1] = t
        else:
            segs.append([t, t, kind])
    tl = "; ".join(f"{a}-{b}s {names[k]}" for a, b, k in segs) or "no frames captured"
    lines = [f"[VM BOOT OBSERVATION - {ph}] The disk of drive {L}: was booted in a throw-away virtual machine "
             f"({'UEFI' if uefi else 'BIOS'}, read-only snapshot, {elapsed}s of {secs}s). This is what a user sees when the PC starts:",
             f"Result: {result[0]} - {result[1]}",
             f"Screen timeline: {tl}"]
    if exited:
        lines.append(f"The VM restarted or shut down by itself at about {elapsed}s (reboot loop or crash).")
    for t, txt in texts:
        lines.append(f"Text read from the screen at about {t}s: {txt}")
    if not texts:
        lines.append("No text could be read from the screen.")
    lines.append("Limits: the VM has no TPM, no GPU drivers and no network; a slow boot in software emulation can look stuck.")
    return "\n".join(lines)


VM_RULES = (
    "\n\n[VM FIRST] Before any fix, a virtual machine booted the disk; the VM BOOT OBSERVATION above shows what really happens "
    "at startup. Read it FIRST and say in one sentence what the symptom is (reboot loop, Automatic Repair screen, blue screen "
    "with its stop code, black screen, or boots fine) and what that implies. THEN use the diagnostics to find the cause and fix it. "
    "If the VM boots normally and the diagnostics show nothing serious, say the boot is fine and only fix real problems. "
    "A very slow boot inside the VM is a limit of the VM, not a PC problem."
)


def extract_cmds(reply):
    """Commands from the ``` code blocks of an AI reply (comments and blank lines skipped)."""
    out = []
    for block in re.findall(r"```[a-zA-Z]*\n(.*?)```", reply, re.S):
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "REM ", "::")):
                out.append(line)
    return out


def relaunch_as_admin():
    params = " ".join(f'"{a}"' for a in sys.argv)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)


def http_json(url, key, payload=None, timeout=90):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "WindowsFixerAI/1.0",
        },
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def gather_system_info():
    lines = [f"Python platform: {platform.platform()}", f"Machine: {platform.machine()}"]
    try:
        total, used, free = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        lines.append(f"System drive: {free // 2**30} GB free of {total // 2**30} GB")
    except Exception:
        pass
    gpu = detect_nvidia_gpu()
    if gpu:
        lines.append(f"NVIDIA GPU: {gpu}")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_OperatingSystem | "
             "Select Caption,Version,LastBootUpTime,FreePhysicalMemory,TotalVisibleMemorySize | Format-List"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        lines.append(out)
    except Exception:
        pass
    return "\n".join(lines)


# Professional Dark/Slate theme palette inspired by modern Windows 11 Fluent utilities
THEME = {
    "bg_root": "#0a0e17",         # Deep obsidian window background
    "bg_header": "#0f1626",       # App header bar
    "bg_card": "#131b2e",         # Elevated card / panel background
    "bg_card_inner": "#1a243b",   # Inner container / card
    "bg_input": "#0c1322",        # Text, Entry, Treeview background
    "border": "#22304d",          # Card and widget borders
    "border_focus": "#38bdf8",    # Focus highlight
    "fg_text": "#f8fafc",         # High-contrast primary text
    "fg_muted": "#94a3b8",        # Secondary text
    "fg_dim": "#64748b",          # Subtle descriptions
    "accent": "#0284c7",          # Primary accent / blue
    "accent_hover": "#0369a1",    # Accent hover
    "accent_fg": "#ffffff",       # Text on accent
    "accent_cyan": "#38bdf8",     # Cyan highlights
    "success": "#10b981",         # Emerald success / fix
    "success_hover": "#059669",
    "warning": "#f59e0b",         # Amber warning / elevation
    "warning_hover": "#d97706",
    "danger": "#ef4444",          # Rose / stop
    "danger_hover": "#dc2626",
    "term_bg": "#060911",         # Terminal background
    "term_fg": "#e2e8f0",         # Terminal text
}

# Enable high-DPI scaling on Windows so text and controls render crisp on high-res displays
if os.name == "nt":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Windows Fixer AI - Professional System Diagnostics & Automated Recovery")
        self.geometry(f"1180x{max(640, min(920, self.winfo_screenheight() - 90))}")
        self.minsize(980, 680)
        self.configure(bg=THEME["bg_root"])
        self.q = queue.Queue()
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.commands = []

        st = load_settings()
        self.pid = st.get("provider") if st.get("provider") in PROVIDERS else DEFAULT_PROVIDER
        saved = st.get("keys") if isinstance(st.get("keys"), dict) else {}
        self.keys = {p: str(saved.get(p, "")).strip() for p in PROVIDERS}
        self.sel = st.get("models") if isinstance(st.get("models"), dict) else {}
        self.remember = bool(st.get("remember", True))
        self.dead = set(st.get("dead_nvidia") or [])   # models NVIDIA answered 404 for on this account
        self.good = list(st.get("good_nvidia") or [])  # models that answered successfully
        self.use_cli_init = bool(st.get("use_cli", bool(find_opencode())))
        self.qemu_path = str(st.get("qemu_path") or "")
        self.vm_on_init = bool(st.get("vm_on", bool(find_qemu(self.qemu_path))))
        try:
            self.vm_secs_init = int(st.get("vm_secs", 120))
        except Exception:
            self.vm_secs_init = 120
        self.vm_shots = {}
        self.vm_report = {}      # phase -> text description of what the VM showed (for the AI)
        self.pre_before = None   # (drive, test rows) when the VM check already ran before the diagnostics
        self.vm_img = None
        self._vm_cfg = {"on": False, "secs": 120, "test": True}
        self.models = {}
        for p, cfg in PROVIDERS.items():
            ids = load_cache(p)
            if p == "opencode":
                ids = [i for i in ids if is_free_name(i)]
            self.models[p] = ids or list(cfg["fallback"])
        self.detected = detect_keys()
        self.autofill(explicit=False)  # fill empty key slots from keys found on this PC
        if not self.keys["opencode"]:
            self.keys["opencode"] = PROVIDERS["opencode"]["default_key"]

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._pump)
        threading.Thread(target=self._gpu_worker, daemon=True).start()
        if self.detected:
            names = ", ".join(PROVIDERS[p]["label"] for p in self.detected)
            self.status.set(f"Auto-detected API key(s) on this PC for: {names}")
        self.refresh_models(all_providers=True)

    # ---------- Theme & Styling ----------
    def _setup_theme(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Notebook & Tabs
        style.configure("TNotebook", background=THEME["bg_root"], borderwidth=0)
        style.configure("TNotebook.Tab", background=THEME["bg_card"], foreground=THEME["fg_muted"],
                        padding=[18, 9], font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", THEME["accent"]), ("active", THEME["bg_card_inner"])],
                  foreground=[("selected", "#ffffff"), ("active", "#ffffff")])

        # Frames
        style.configure("TFrame", background=THEME["bg_root"])
        style.configure("Root.TFrame", background=THEME["bg_root"])
        style.configure("Card.TFrame", background=THEME["bg_card"])
        style.configure("Inner.TFrame", background=THEME["bg_card_inner"])
        style.configure("Header.TFrame", background=THEME["bg_header"])

        # Labels
        style.configure("TLabel", background=THEME["bg_card"], foreground=THEME["fg_text"], font=("Segoe UI", 9))
        style.configure("Root.TLabel", background=THEME["bg_root"], foreground=THEME["fg_text"], font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=THEME["bg_header"], foreground=THEME["accent_cyan"], font=("Segoe UI", 12, "bold"))
        style.configure("HeaderSub.TLabel", background=THEME["bg_header"], foreground=THEME["fg_dim"], font=("Segoe UI", 8))
        style.configure("Muted.TLabel", background=THEME["bg_card"], foreground=THEME["fg_muted"], font=("Segoe UI", 9))
        style.configure("Dim.TLabel", background=THEME["bg_card"], foreground=THEME["fg_dim"], font=("Segoe UI", 8))
        style.configure("Status.TLabel", background=THEME["bg_root"], foreground=THEME["fg_muted"], font=("Segoe UI", 8))

        # Labelframes
        style.configure("TLabelframe", background=THEME["bg_card"], bordercolor=THEME["border"],
                        darkcolor=THEME["border"], lightcolor=THEME["border"], relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=THEME["bg_card"], foreground=THEME["accent_cyan"],
                        font=("Segoe UI", 9, "bold"))

        # Buttons
        style.configure("TButton", background=THEME["bg_card_inner"], foreground=THEME["fg_text"],
                        bordercolor=THEME["border"], darkcolor=THEME["border"], lightcolor=THEME["border"],
                        font=("Segoe UI", 9, "bold"), padding=[10, 5])
        style.map("TButton",
                  background=[("pressed", THEME["accent"]), ("active", "#253456"), ("disabled", "#141c2b")],
                  foreground=[("disabled", "#4b5563")])

        style.configure("Accent.TButton", background=THEME["accent"], foreground="#ffffff",
                        bordercolor=THEME["border"], font=("Segoe UI", 9, "bold"), padding=[12, 6])
        style.map("Accent.TButton",
                  background=[("pressed", "#026aa2"), ("active", THEME["accent_hover"]), ("disabled", "#1e3a5f")],
                  foreground=[("disabled", "#64748b")])

        style.configure("Success.TButton", background=THEME["success"], foreground="#ffffff",
                        bordercolor=THEME["border"], font=("Segoe UI", 9, "bold"), padding=[12, 6])
        style.map("Success.TButton",
                  background=[("pressed", "#047857"), ("active", THEME["success_hover"]), ("disabled", "#0f392b")],
                  foreground=[("disabled", "#64748b")])

        style.configure("Danger.TButton", background=THEME["danger"], foreground="#ffffff",
                        bordercolor=THEME["border"], font=("Segoe UI", 9, "bold"), padding=[10, 5])
        style.map("Danger.TButton",
                  background=[("pressed", "#b91c1c"), ("active", THEME["danger_hover"]), ("disabled", "#3a171d")],
                  foreground=[("disabled", "#64748b")])

        style.configure("Warning.TButton", background=THEME["warning"], foreground="#ffffff",
                        bordercolor=THEME["border"], font=("Segoe UI", 9, "bold"), padding=[10, 5])
        style.map("Warning.TButton",
                  background=[("pressed", "#b45309"), ("active", THEME["warning_hover"]), ("disabled", "#3a2a17")],
                  foreground=[("disabled", "#64748b")])

        # Entry & Combobox
        style.configure("TEntry", fieldbackground=THEME["bg_input"], foreground=THEME["fg_text"],
                        insertcolor=THEME["accent_cyan"], bordercolor=THEME["border"], padding=[6, 4])
        style.configure("TCombobox", fieldbackground=THEME["bg_input"], background=THEME["bg_card_inner"],
                        foreground=THEME["fg_text"], bordercolor=THEME["border"], arrowcolor=THEME["fg_muted"], padding=[6, 4])
        style.map("TCombobox",
                  fieldbackground=[("readonly", THEME["bg_input"])],
                  foreground=[("readonly", THEME["fg_text"])],
                  selectbackground=[("readonly", THEME["accent"])])

        # Checkbutton
        style.configure("TCheckbutton", background=THEME["bg_card"], foreground=THEME["fg_text"], font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", THEME["bg_card"])],
                  indicatorcolor=[("selected", THEME["accent_cyan"]), ("!selected", THEME["bg_input"])])

        # Spinbox
        style.configure("TSpinbox", fieldbackground=THEME["bg_input"], background=THEME["bg_card_inner"],
                        foreground=THEME["fg_text"], bordercolor=THEME["border"])

        # Treeview
        style.configure("Treeview", background=THEME["bg_input"], fieldbackground=THEME["bg_input"],
                        foreground=THEME["fg_text"], bordercolor=THEME["border"], rowheight=24, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=THEME["bg_card_inner"], foreground=THEME["accent_cyan"],
                        bordercolor=THEME["border"], font=("Segoe UI", 9, "bold"), padding=[6, 4])

        # Scrollbar
        style.configure("TScrollbar", background=THEME["bg_card_inner"], troughcolor=THEME["bg_input"],
                        bordercolor=THEME["border"], arrowcolor=THEME["fg_muted"])

        def _fixed_map(opt):
            return [e for e in style.map("Treeview", query_opt=opt) if e[:2] != ("!disabled", "!selected")]
        style.map("Treeview", foreground=_fixed_map("foreground"), background=_fixed_map("background"))

    # ---------- UI ----------
    def _build(self):
        self._setup_theme()
        self.main_canvas = None  # Backward compatibility for _wheel handler

        # Top Header Bar
        hdr = ttk.Frame(self, style="Header.TFrame", padding=(12, 8))
        hdr.pack(fill="x", side="top")

        hdr_left = ttk.Frame(hdr, style="Header.TFrame")
        hdr_left.pack(side="left")
        ttk.Label(hdr_left, text="🛠  WINDOWS FIXER AI", style="Header.TLabel").pack(anchor="w")
        ttk.Label(hdr_left, text="Professional System Diagnostics & Automated Recovery Suite", style="HeaderSub.TLabel").pack(anchor="w")

        # Header Center: Quick Status Badges
        hdr_mid = ttk.Frame(hdr, style="Header.TFrame")
        hdr_mid.pack(side="left", padx=24)

        self.hdr_badge_frame = ttk.Frame(hdr_mid, style="Inner.TFrame", padding=(8, 4))
        self.hdr_badge_frame.pack()

        self.hdr_model_lbl = ttk.Label(self.hdr_badge_frame, text=f"AI Engine: {PROVIDERS[self.pid]['label']}",
                                       foreground=THEME["accent_cyan"], background=THEME["bg_card_inner"],
                                       font=("Segoe UI", 9, "bold"))
        self.hdr_model_lbl.pack(side="left", padx=6)

        ttk.Label(self.hdr_badge_frame, text="•", foreground=THEME["fg_dim"],
                  background=THEME["bg_card_inner"]).pack(side="left", padx=2)

        self.hdr_gpu_lbl = ttk.Label(self.hdr_badge_frame, text="GPU: Checking...",
                                     foreground=THEME["fg_text"], background=THEME["bg_card_inner"],
                                     font=("Segoe UI", 8))
        self.hdr_gpu_lbl.pack(side="left", padx=6)

        # Header Right: Admin Status Badge & Actions
        hdr_right = ttk.Frame(hdr, style="Header.TFrame")
        hdr_right.pack(side="right")

        admin_active = is_admin()
        admin_bg = "#064e3b" if admin_active else "#78350f"
        admin_fg = "#34d399" if admin_active else "#fbbf24"
        admin_txt = "🛡 Administrator" if admin_active else "⚠ Standard User"

        self.hdr_admin_lbl = tk.Label(hdr_right, text=admin_txt, bg=admin_bg, fg=admin_fg,
                                      font=("Segoe UI", 8, "bold"), padx=8, pady=3, relief="flat")
        self.hdr_admin_lbl.pack(side="left", padx=(0, 8))

        if not admin_active and os.name == "nt":
            ttk.Button(hdr_right, text="Restart as Admin", command=self.go_admin,
                       style="Warning.TButton").pack(side="left", padx=(0, 6))

        ttk.Button(hdr_right, text="⟲ New Session", command=self.reset_chat,
                   style="TButton").pack(side="left")

        # Bottom Status Bar (Fixed at bottom)
        self.status = tk.StringVar(value="Ready")
        status_frame = ttk.Frame(self, style="Root.TFrame", padding=(10, 4))
        status_frame.pack(fill="x", side="bottom")

        ttk.Label(status_frame, textvariable=self.status, style="Status.TLabel", anchor="w").pack(side="left", fill="x", expand=True)
        os_info = f"Windows Fixer AI v14 • Python {platform.python_version()} • {platform.system()} {platform.release()}"
        ttk.Label(status_frame, text=os_info, style="Status.TLabel", anchor="e").pack(side="right")

        # Bottom Docked Activity Console
        dock = ttk.Frame(self, style="Card.TFrame", padding=(8, 4))
        dock.pack(fill="x", side="bottom", padx=8, pady=(0, 4))

        dock_hdr = ttk.Frame(dock, style="Card.TFrame")
        dock_hdr.pack(fill="x", pady=(0, 4))
        ttk.Label(dock_hdr, text="●  EXECUTION CONSOLE & ACTIVITY LOG", foreground=THEME["accent_cyan"],
                  font=("Segoe UI", 9, "bold")).pack(side="left")

        ttk.Button(dock_hdr, text="Clear Console", command=lambda: self.log.delete("1.0", "end"),
                   style="TButton").pack(side="right", padx=(4, 0))
        ttk.Button(dock_hdr, text="Copy Output", command=self.copy_log,
                   style="TButton").pack(side="right")

        self.log = ScrolledText(dock, height=6, wrap="word", bg=THEME["term_bg"], fg=THEME["term_fg"],
                                insertbackground=THEME["accent_cyan"], relief="solid", bd=1,
                                highlightthickness=1, highlightbackground=THEME["border"],
                                highlightcolor=THEME["border_focus"], font=("Consolas", 9))
        self.log.pack(fill="both", expand=False)

        # Tabbed Navigation Notebook (Center workspace)
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(4, 4))

        self._build_tab_assistant()
        self._build_tab_fullscan()
        self._build_tab_storage()
        self._build_tab_vm()
        self._build_tab_settings()

    def _build_tab_assistant(self):
        tab = ttk.Frame(self.nb, style="Root.TFrame", padding=6)
        self.nb.add(tab, text="💬  AI Assistant & Diagnostics")

        tab.columnconfigure(0, weight=6)
        tab.columnconfigure(1, weight=4)
        tab.rowconfigure(0, weight=0)
        tab.rowconfigure(1, weight=1)

        # --- Left Column: Problem Input & AI Response ---
        # 1. Problem Input
        in_card = ttk.LabelFrame(tab, text=" Describe Windows Problem or Symptoms ", padding=8)
        in_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))

        ttk.Label(in_card, text="Type what is going wrong (e.g. WiFi dropping, BSOD CRITICAL_PROCESS_DIED, slow boot, audio missing):",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 4))

        self.problem = tk.Text(in_card, height=4, wrap="word", bg=THEME["bg_input"], fg=THEME["fg_text"],
                               insertbackground=THEME["accent_cyan"], relief="solid", bd=1,
                               highlightthickness=1, highlightbackground=THEME["border"],
                               highlightcolor=THEME["border_focus"], font=("Segoe UI", 10))
        self.problem.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(in_card, style="Card.TFrame")
        row.pack(fill="x")

        self.sysinfo_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Include basic system hardware & OS info", variable=self.sysinfo_var).pack(side="left")

        self.stop_btn = ttk.Button(row, text="⏹ Stop", command=self.autofix_stop_now, style="Danger.TButton", state="disabled")
        self.stop_btn.pack(side="right", padx=(4, 0))

        self.fix_btn = ttk.Button(row, text="🚀 Auto-Fix with AI", command=self.autofix, style="Success.TButton")
        self.fix_btn.pack(side="right", padx=4)

        self.ask_btn = ttk.Button(row, text="⚡ Diagnose with AI", command=self.ask, style="Accent.TButton")
        self.ask_btn.pack(side="right")

        # 2. AI Response
        ans_card = ttk.LabelFrame(tab, text=" AI Diagnostic Analysis & Repair Plan ", padding=8)
        ans_card.grid(row=1, column=0, sticky="nsew", padx=(0, 6))

        ans_bar = ttk.Frame(ans_card, style="Card.TFrame")
        ans_bar.pack(fill="x", pady=(0, 4))
        ttk.Label(ans_bar, text="Step-by-step diagnostic reasoning and recovery plan:", style="Muted.TLabel").pack(side="left")
        ttk.Button(ans_bar, text="📄 Export Report (.html)", command=self.export_report, style="TButton").pack(side="right")
        ttk.Button(ans_bar, text="📋 Copy Plan", command=self.copy_answer, style="TButton").pack(side="right", padx=(0, 6))

        self.answer = ScrolledText(ans_card, wrap="word", bg=THEME["bg_input"], fg=THEME["fg_text"],
                                   insertbackground=THEME["accent_cyan"], relief="solid", bd=1,
                                   highlightthickness=1, highlightbackground=THEME["border"],
                                   highlightcolor=THEME["border_focus"], font=("Consolas", 10))
        self.answer.pack(fill="both", expand=True)

        # --- Right Column: Commands & Quick Fixes ---
        r_col = ttk.Frame(tab, style="Root.TFrame")
        r_col.grid(row=0, column=1, rowspan=2, sticky="nsew")
        r_col.rowconfigure(0, weight=1)
        r_col.rowconfigure(1, weight=1)
        r_col.columnconfigure(0, weight=1)

        # Commands Card
        cmd_card = ttk.LabelFrame(r_col, text=" AI Suggested Commands ", padding=8)
        cmd_card.grid(row=0, column=0, sticky="nsew", pady=(0, 6))

        ttk.Label(cmd_card, text="Commands parsed from AI response (select to execute):", style="Muted.TLabel").pack(anchor="w", pady=(0, 4))

        cf = ttk.Frame(cmd_card, style="Card.TFrame")
        cf.pack(fill="both", expand=True)
        self.cmd_list = tk.Listbox(cf, height=7, selectmode="extended", exportselection=False,
                                   bg=THEME["bg_input"], fg=THEME["accent_cyan"],
                                   selectbackground=THEME["accent"], selectforeground="#ffffff",
                                   relief="solid", bd=1, highlightthickness=1,
                                   highlightbackground=THEME["border"], highlightcolor=THEME["border_focus"],
                                   font=("Consolas", 9))
        csb = ttk.Scrollbar(cf, orient="vertical", command=self.cmd_list.yview)
        self.cmd_list.configure(yscrollcommand=csb.set)
        self.cmd_list.pack(side="left", fill="both", expand=True)
        csb.pack(side="left", fill="y")

        ttk.Button(cmd_card, text="▶ Run Selected Command(s)", command=self.run_selected,
                   style="Accent.TButton").pack(fill="x", pady=(6, 0))

        # Quick Fixes Card
        q_card = ttk.LabelFrame(r_col, text=" Quick Fix Library (55 Presets) ", padding=8)
        q_card.grid(row=1, column=0, sticky="nsew")

        ttk.Label(q_card, text="Filter by category or live search:", style="Muted.TLabel").pack(anchor="w", pady=(0, 2))

        q_filter_frame = ttk.Frame(q_card, style="Card.TFrame")
        q_filter_frame.pack(fill="x", pady=(0, 4))

        self.qcat_var = tk.StringVar(value="All")
        qcat = ttk.Combobox(q_filter_frame, textvariable=self.qcat_var, state="readonly", width=16,
                            values=["All"] + list(QUICK_FIX_GROUPS))
        qcat.pack(side="left", padx=(0, 4))
        qcat.bind("<<ComboboxSelected>>", self._fill_quick)

        self.qsearch_var = tk.StringVar()
        q_search = ttk.Entry(q_filter_frame, textvariable=self.qsearch_var)
        q_search.pack(side="left", fill="x", expand=True)
        self.qsearch_var.trace_add("write", self._fill_quick)

        qf = ttk.Frame(q_card, style="Card.TFrame")
        qf.pack(fill="both", expand=True)
        self.quick = tk.Listbox(qf, height=8, selectmode="extended", exportselection=False,
                                bg=THEME["bg_input"], fg=THEME["fg_text"],
                                selectbackground=THEME["accent"], selectforeground="#ffffff",
                                relief="solid", bd=1, highlightthickness=1,
                                highlightbackground=THEME["border"], highlightcolor=THEME["border_focus"],
                                font=("Segoe UI", 9))
        qsb = ttk.Scrollbar(qf, orient="vertical", command=self.quick.yview)
        self.quick.configure(yscrollcommand=qsb.set)
        self.quick.pack(side="left", fill="both", expand=True)
        qsb.pack(side="left", fill="y")
        self._fill_quick()

        ttk.Button(q_card, text="⚡ Run Selected Quick Fix(es)", command=self.run_quick,
                   style="Success.TButton").pack(fill="x", pady=(6, 0))

    def _build_tab_fullscan(self):
        tab = ttk.Frame(self.nb, style="Root.TFrame", padding=12)
        self.nb.add(tab, text="🛡  Full PC Health Scan")

        # Hero Card: 12-point health overview
        hero = ttk.LabelFrame(tab, text=" Comprehensive 12-Point Whole-PC Health & Diagnostic Scan ", padding=12)
        hero.pack(fill="x", pady=(0, 10))

        ttk.Label(hero, text="One-Click Deep Diagnostic Across All Vital Windows Subsystems",
                  font=("Segoe UI", 11, "bold"), foreground=THEME["accent_cyan"]).pack(anchor="w", pady=(0, 4))
        ttk.Label(hero, text="Don't know what is causing the problem? The Full PC Scan performs an exhaustive read-only inspection of "
                            "system file integrity, storage health, network connectivity, Windows Update components, device drivers, "
                            "crash dumps, event viewer logs, security status, and startup services.",
                  wraplength=950, justify="left", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        pills = ttk.Frame(hero, style="Card.TFrame")
        pills.pack(fill="x")
        points = [
            "✓ System Files (SFC & DISM)", "✓ Storage & SMART Health", "✓ Network & Winsock Stack",
            "✓ Windows Update Services", "✓ Device Drivers & Hardware", "✓ BSOD & Crash Dumps",
            "✓ Event Logs & Errors", "✓ Defender & Security Center", "✓ Startup Programs & Bloat",
            "✓ Print Spooler Service", "✓ Time & RTC Clock Sync", "✓ Memory & Pagefile Health"
        ]
        for idx, pt in enumerate(points):
            r, c = divmod(idx, 4)
            lbl = tk.Label(pills, text=pt, bg=THEME["bg_card_inner"], fg=THEME["fg_text"],
                           font=("Segoe UI", 9), padx=10, pady=4, relief="flat")
            lbl.grid(row=r, column=c, padx=4, pady=3, sticky="w")

        # Two Main Action Cards
        action_row = ttk.Frame(tab, style="Root.TFrame")
        action_row.pack(fill="both", expand=True, pady=(0, 10))
        action_row.columnconfigure(0, weight=1)
        action_row.columnconfigure(1, weight=1)

        # Card 1: Read-Only Scan
        c1 = ttk.LabelFrame(action_row, text=" Option A: Read-Only System Scan & AI Diagnosis ", padding=12)
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        ttk.Label(c1, text="🔍 Safe Diagnostic Inspection", font=("Segoe UI", 10, "bold"),
                  foreground=THEME["accent_cyan"]).pack(anchor="w", pady=(0, 4))
        ttk.Label(c1, text="• Gathers deep diagnostic data across all 12 subsystems.\n"
                            "• Completely read-only: does not modify any system files or registry.\n"
                            "• Generates an AI-powered summary explaining every issue found.",
                  wraplength=440, justify="left", style="Muted.TLabel").pack(anchor="w", pady=(0, 12))

        self.fullscan_btn = ttk.Button(c1, text="🔍 Scan Whole PC + Explain (Read-Only)",
                                       command=lambda: self.full_scan(False), style="Accent.TButton")
        self.fullscan_btn.pack(anchor="w", pady=(6, 0))

        # Card 2: Auto-Fix Everything
        c2 = ttk.LabelFrame(action_row, text=" Option B: Full PC Scan + Automated AI Deep Repair ", padding=12)
        c2.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        ttk.Label(c2, text="🛠 Automated AI Multi-Round Repair", font=("Segoe UI", 10, "bold"),
                  foreground=THEME["success"]).pack(anchor="w", pady=(0, 4))
        ttk.Label(c2, text="• Step 1: Creates a System Restore Point first for total rollback safety.\n"
                            "• Step 2: Executes full 12-point scan.\n"
                            "• Step 3: AI fixes identified issues group-by-group, verifying each step.",
                  wraplength=440, justify="left", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        self.restore_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(c2, text="Create a System Restore Point first (Recommended, requires Admin)",
                        variable=self.restore_var).pack(anchor="w", pady=(0, 8))

        self.fullfix_btn = ttk.Button(c2, text="🛠 Scan Whole PC + AUTO-FIX Everything with AI",
                                      command=lambda: self.full_scan(True), style="Success.TButton")
        self.fullfix_btn.pack(anchor="w", pady=(6, 4))

        ttk.Button(c2, text="🛡 Create System Restore Point Now", command=self.create_restore_point_now,
                   style="TButton").pack(anchor="w")

        # Safety Safeguards Banner
        saf = ttk.LabelFrame(tab, text=" Safety & Reliability Safeguards ", padding=10)
        saf.pack(fill="x")
        ttk.Label(saf, text="🔒 Strict Safety Rules: Format, diskpart clean, partition deletions, and destructive registry deletes are strictly blocked. "
                            "Every command runs non-interactively with a 30-minute safety timeout. Run as Administrator for full repair capability.",
                  wraplength=950, justify="left", style="Dim.TLabel").pack(anchor="w")

    def _build_tab_storage(self):
        tab = ttk.Frame(self.nb, style="Root.TFrame", padding=12)
        self.nb.add(tab, text="💾  Storage & Boot Repair")

        # Hard Disk Doctor
        dk = ttk.LabelFrame(tab, text=" Hard Disk & Storage Volume Doctor ", padding=12)
        dk.pack(fill="x", pady=(0, 10))

        ttk.Label(dk, text="Inspect, check SMART health, and repair file system corruption or bad sectors on any drive:",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        r0 = ttk.Frame(dk, style="Card.TFrame")
        r0.pack(fill="x", pady=(0, 8))

        ttk.Label(r0, text="Target Drive:").pack(side="left", padx=(0, 6))
        self.drive_var = tk.StringVar()
        self.drive_box = ttk.Combobox(r0, textvariable=self.drive_var, state="readonly", width=28)
        self.drive_box.pack(side="left", padx=(0, 8))

        ttk.Button(r0, text="🔄 Refresh Drives", command=self.refresh_drives).pack(side="left", padx=(0, 8))

        r1 = ttk.Frame(dk, style="Card.TFrame")
        r1.pack(fill="x", pady=(4, 0))

        self.disk_btn = ttk.Button(r1, text="🔍 Auto-Check Disk (Health & SMART)", command=self.auto_disk,
                                   style="Accent.TButton")
        self.disk_btn.pack(side="left", padx=(0, 8))

        self.diskfix_btn = ttk.Button(r1, text="🤖 Auto-Fix Drive with AI", command=self.auto_fix_disk,
                                      style="Success.TButton")
        self.diskfix_btn.pack(side="left", padx=(0, 8))

        ttk.Button(r1, text="🔧 Standard Repair (chkdsk /f)", command=lambda: self.repair(False)).pack(side="left", padx=(0, 8))
        ttk.Button(r1, text="🛡 Deep Surface Scan (chkdsk /r)", command=lambda: self.repair(True)).pack(side="left", padx=(0, 8))
        ttk.Button(r1, text="🛡 Create Restore Point", command=self.create_restore_point_now, style="TButton").pack(side="left")

        # Boot Loop & Startup Repair
        bt = ttk.LabelFrame(tab, text=" Windows Startup & 'Automatic Repair' Loop Recovery ", padding=12)
        bt.pack(fill="x", pady=(0, 10))

        ttk.Label(bt, text="Fix systems trapped in 'Automatic Repair' loops, BCD errors, corrupted registry hives, or blue screens at startup:",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        b0 = ttk.Frame(bt, style="Card.TFrame")
        b0.pack(fill="x", pady=(0, 8))

        ttk.Label(b0, text="Drive with Windows to Fix:").pack(side="left", padx=(0, 6))
        self.boot_drive_var = tk.StringVar()
        self.boot_drive_box = ttk.Combobox(b0, textvariable=self.boot_drive_var, state="readonly", width=55)
        self.boot_drive_box.pack(side="left", padx=(0, 8))

        ttk.Button(b0, text="🔄 Refresh Drives", command=self.refresh_drives).pack(side="left")

        b1 = ttk.Frame(bt, style="Card.TFrame")
        b1.pack(fill="x", pady=(4, 8))

        self.boot_btn = ttk.Button(b1, text="🚀 Fix Repair Loop on Selected Drive with AI", command=self.boot_fix,
                                   style="Success.TButton")
        self.boot_btn.pack(side="left", padx=(0, 8))

        ttk.Button(b1, text="📄 Generate WinRE Offline Script (.bat)", command=self.make_winre_script,
                   style="Accent.TButton").pack(side="left", padx=(0, 8))

        ttk.Button(b1, text="🔍 Analyze Boot Log with AI...", command=self.analyze_log).pack(side="left")

        ttk.Label(bt, text="💡 Recovery Tip: If you connected a broken PC's hard drive to this PC using a USB-to-SATA adapter, "
                            "select that drive letter above. If the PC cannot boot into Windows at all and you have no spare PC, "
                            "click 'Generate WinRE Offline Script (.bat)' and run it from the Windows Recovery Command Prompt.",
                  wraplength=950, justify="left", style="Dim.TLabel").pack(anchor="w")

        self.refresh_drives()

    def _build_tab_vm(self):
        tab = ttk.Frame(self.nb, style="Root.TFrame", padding=12)
        self.nb.add(tab, text="🖥  VM Sandbox & Auto-Test")

        tab.columnconfigure(0, weight=6)
        tab.columnconfigure(1, weight=4)
        tab.rowconfigure(0, weight=1)

        # Left Column: Auto-Test Matrix
        tp = ttk.LabelFrame(tab, text=" Auto-Test Verification Matrix ", padding=10)
        tp.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        ttk.Label(tp, text="Automated pre/post fix verification testing:", style="Muted.TLabel").pack(anchor="w", pady=(0, 4))

        tf = ttk.Frame(tp, style="Card.TFrame")
        tf.pack(fill="both", expand=True)

        self.test_tree = ttk.Treeview(tf, columns=("before", "after", "detail"), height=8)
        for col, txt, w, stretch in (("#0", "Test Name", 160, False), ("before", "Before Fix", 85, False),
                                     ("after", "After Fix", 85, False), ("detail", "Diagnostic Detail", 300, True)):
            self.test_tree.heading(col, text=txt, anchor="w")
            self.test_tree.column(col, width=w, stretch=stretch, anchor="w")

        for tag, colour in (("ok", "#34d399"), ("warn", "#fbbf24"), ("fail", "#f87171"), ("skip", "#94a3b8")):
            self.test_tree.tag_configure(tag, foreground=colour)

        tsb = ttk.Scrollbar(tf, orient="vertical", command=self.test_tree.yview)
        self.test_tree.configure(yscrollcommand=tsb.set)
        self.test_tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="left", fill="y")

        # Summary & Actions
        tr = ttk.Frame(tp, style="Card.TFrame")
        tr.pack(fill="x", pady=(8, 0))

        self.test_state_var = tk.StringVar(value="No test executed yet. Runs automatically before and after each AI fix.")
        ttk.Label(tr, textvariable=self.test_state_var, style="Muted.TLabel").pack(anchor="w")

        self.test_sum_var = tk.StringVar(value="")
        ttk.Label(tr, textvariable=self.test_sum_var, font=("Segoe UI", 9, "bold"), foreground=THEME["accent_cyan"]).pack(anchor="w", pady=2)

        ctrl_row = ttk.Frame(tr, style="Card.TFrame")
        ctrl_row.pack(fill="x", pady=(4, 0))

        self.test_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctrl_row, text="Auto-test before and after each AI fix", variable=self.test_var).pack(side="left")

        self.test_btn = ttk.Button(ctrl_row, text="🧪 Test Selected Drive Now", command=self.test_now,
                                   style="Accent.TButton")
        self.test_btn.pack(side="right")

        self.test_counts = {"before": None, "after": None}

        # Right Column: QEMU Sandbox Display
        vf = ttk.LabelFrame(tab, text=" QEMU Virtual Machine Sandbox ", padding=10)
        vf.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        disp_frame = tk.Frame(vf, bg="#000000", bd=2, relief="solid")
        disp_frame.pack(pady=(0, 6))

        self.vm_canvas = tk.Canvas(disp_frame, width=272, height=204, bg="#000000",
                                   highlightthickness=0)
        self.vm_canvas.pack()
        self.vm_canvas.create_text(136, 102, fill="#64748b", justify="center",
                                   text="Virtual Machine Screen Preview\n(Boots drive in isolated sandbox)")

        self.vm_cap = tk.StringVar(value="VM: not running")
        ttk.Label(vf, textvariable=self.vm_cap, style="Muted.TLabel").pack(anchor="w")

        # Snapshots Bar
        bb = ttk.Frame(vf, style="Card.TFrame")
        bb.pack(fill="x", pady=(4, 8))
        for txt, ph in (("Before", "before"), ("After", "after"), ("Manual", "now")):
            ttk.Button(bb, text=txt, width=7, command=lambda p=ph: self.show_shot(p)).pack(side="left", padx=(0, 4))
        ttk.Button(bb, text="💾 Save Picture", command=self.save_shot).pack(side="left")

        # VM Controls
        vctl = ttk.Frame(vf, style="Card.TFrame")
        vctl.pack(fill="x", pady=(4, 0))

        self.vm_var = tk.BooleanVar(value=self.vm_on_init)
        ttk.Checkbutton(vctl, text="Also boot drive in virtual machine (QEMU)", variable=self.vm_var).pack(anchor="w")

        sr = ttk.Frame(vctl, style="Card.TFrame")
        sr.pack(anchor="w", pady=4)
        ttk.Label(sr, text="VM run time (seconds):").pack(side="left", padx=(0, 4))
        self.vm_secs_var = tk.IntVar(value=self.vm_secs_init)
        ttk.Spinbox(sr, from_=30, to=600, increment=30, width=5, textvariable=self.vm_secs_var).pack(side="left")

        vbtns = ttk.Frame(vctl, style="Card.TFrame")
        vbtns.pack(fill="x", pady=4)
        self.vm_btn = ttk.Button(vbtns, text="▶ Boot Drive in VM Now", command=self.vm_now,
                                 style="Success.TButton")
        self.vm_btn.pack(side="left", padx=(0, 4))
        ttk.Button(vbtns, text="⏹ Stop VM", command=self.vm_stop.set, style="Danger.TButton").pack(side="left")

        qtools = ttk.Frame(vctl, style="Card.TFrame")
        qtools.pack(fill="x", pady=(4, 0))
        ttk.Button(qtools, text="Install QEMU", command=self.install_qemu).pack(side="left", padx=(0, 4))
        ttk.Button(qtools, text="Locate QEMU...", command=self.locate_qemu).pack(side="left")

        self.qemu_lbl = tk.StringVar()
        ttk.Label(vf, textvariable=self.qemu_lbl, wraplength=280, style="Dim.TLabel").pack(anchor="w", pady=(6, 0))

        self._qemu_label()
        self.vm_var.trace_add("write", self._vm_cfg_update)
        self.vm_secs_var.trace_add("write", self._vm_cfg_update)
        self.test_var.trace_add("write", self._vm_cfg_update)
        self._vm_cfg_update()

    def _build_tab_settings(self):
        tab = ttk.Frame(self.nb, style="Root.TFrame", padding=12)
        self.nb.add(tab, text="⚙  Settings & Providers")

        # Provider & Model Settings
        top = ttk.LabelFrame(tab, text=" AI Intelligence Engine & Model Configuration ", padding=12)
        top.pack(fill="x", pady=(0, 10))

        r0 = ttk.Frame(top, style="Card.TFrame")
        r0.pack(fill="x", pady=(0, 8))

        ttk.Label(r0, text="Provider:").pack(side="left", padx=(0, 6))
        self.prov_var = tk.StringVar(value=PROVIDERS[self.pid]["label"])
        self.prov_box = ttk.Combobox(r0, textvariable=self.prov_var, state="readonly", width=26,
                                     values=[p["label"] for p in PROVIDERS.values()])
        self.prov_box.pack(side="left", padx=(0, 14))
        self.prov_box.bind("<<ComboboxSelected>>", self.on_provider)

        ttk.Label(r0, text="Model:").pack(side="left", padx=(0, 6))
        vals = self.models[self.pid]
        self.model_var = tk.StringVar(value=self.sel.get(self.pid) or vals[0])
        self.model_box = ttk.Combobox(r0, textvariable=self.model_var, values=vals, width=34)
        self.model_box.pack(side="left", padx=(0, 10))

        ttk.Button(r0, text="🔄 Auto-Get Models", command=self.refresh_models).pack(side="left", padx=(0, 6))
        ttk.Button(r0, text="⚡ Test Connection", command=self.test_ai, style="Accent.TButton").pack(side="left")

        # OpenCode CLI Row
        r2 = ttk.Frame(top, style="Card.TFrame")
        r2.pack(fill="x", pady=(4, 0))

        self.cli_var = tk.StringVar(value="OpenCode CLI: " + ("installed" if find_opencode() else "not installed"))
        ttk.Label(r2, textvariable=self.cli_var, style="Muted.TLabel").pack(side="left", padx=(0, 8))

        ttk.Button(r2, text="Install OpenCode CLI on this PC", command=self.install_opencode).pack(side="left", padx=(0, 8))

        self.use_cli = tk.BooleanVar(value=self.use_cli_init)
        ttk.Checkbutton(r2, text="Prefer OpenCode CLI (OpenCode provider only)", variable=self.use_cli).pack(side="left")

        # API Credentials Card
        sec = ttk.LabelFrame(tab, text=" API Authentication Credentials ", padding=12)
        sec.pack(fill="x", pady=(0, 10))

        r1 = ttk.Frame(sec, style="Card.TFrame")
        r1.pack(fill="x", pady=(0, 8))

        self.keylbl = tk.StringVar()
        ttk.Label(r1, textvariable=self.keylbl, width=24).pack(side="left", padx=(0, 6))

        self.key_var = tk.StringVar(value=self.keys.get(self.pid, ""))
        self.key_entry = ttk.Entry(r1, textvariable=self.key_var, show="*", width=38)
        self.key_entry.pack(side="left", padx=(0, 8))

        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text="Show", variable=self.show_var,
                        command=lambda: self.key_entry.config(show="" if self.show_var.get() else "*")).pack(side="left", padx=(0, 8))

        ttk.Button(r1, text="💾 Save Key", command=self.save_key, style="Success.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(r1, text="🔍 Auto-Detect on this PC", command=self.detect_now, style="Accent.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(r1, text="🔗 Get a Key", command=self.open_key_page).pack(side="left")

        self.remember_var = tk.BooleanVar(value=self.remember)
        ttk.Checkbutton(sec, text="Remember API credentials on this PC", variable=self.remember_var).pack(anchor="w")

        # System Hardware & Elevation
        sys_card = ttk.LabelFrame(tab, text=" System Diagnostics & Elevation ", padding=12)
        sys_card.pack(fill="x")

        self.gpu_var = tk.StringVar(value="NVIDIA GPU: detecting...")
        ttk.Label(sys_card, textvariable=self.gpu_var, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 6))

        admin_txt = "Running as Administrator (Full privilege active)" if is_admin() else "Running as Standard User (Restricted privilege - click Restart as Admin)"
        self.admin_lbl = ttk.Label(sys_card, text=admin_txt, font=("Segoe UI", 9, "bold"),
                                   foreground="#10b981" if is_admin() else "#f59e0b")
        self.admin_lbl.pack(anchor="w", pady=(0, 4))

        if not is_admin() and os.name == "nt":
            ttk.Button(sys_card, text="🛡 Restart App as Administrator", command=self.go_admin,
                       style="Warning.TButton").pack(anchor="w", pady=(4, 0))

        self._update_labels()

    def _wheel(self, e):
        """Mouse wheel scrolls the page, except over boxes that scroll themselves."""
        try:
            if e.widget.winfo_class() in ("Text", "Listbox", "Treeview", "TCombobox", "TSpinbox"):
                return
        except Exception:
            return
        if getattr(self, "main_canvas", None):
            try:
                self.main_canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")
            except Exception:
                pass

    def copy_log(self):
        try:
            content = self.log.get("1.0", "end").strip()
            if content:
                self.clipboard_clear()
                self.clipboard_append(content)
                self.status.set("Console log copied to clipboard.")
        except Exception:
            pass

    # ---------- helpers ----------
    def say(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def go_admin(self):
        if messagebox.askyesno("Administrator", "Restart this app with Administrator rights?"):
            relaunch_as_admin()
            self.destroy()

    def reset_chat(self):
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.answer.delete("1.0", "end")
        self.cmd_list.delete(0, "end")
        self.commands = []
        self.status.set("New chat started")

    # ---------- models / AI ----------
    def _update_labels(self):
        self.keylbl.set("OpenCode API key:" if self.pid == "opencode" else "NVIDIA API key (nvapi-...):")
        if hasattr(self, "hdr_model_lbl"):
            self.hdr_model_lbl.config(text=f"AI Engine: {PROVIDERS[self.pid]['label']}")

    def _stash(self):
        """Remember what is currently typed for the active provider."""
        self.keys[self.pid] = self.key_var.get().strip()
        self.sel[self.pid] = self.model_var.get().strip()

    def cur_key(self):
        return self.key_var.get().strip() or PROVIDERS[self.pid]["default_key"]

    def save_all(self):
        self._stash()
        save_settings({
            "provider": self.pid,
            "models": self.sel,
            "use_cli": self.use_cli.get(),
            "remember": self.remember_var.get(),
            "keys": self.keys if self.remember_var.get() else {},
            "dead_nvidia": sorted(self.dead),
            "good_nvidia": self.good[:20],
            "qemu_path": self.qemu_path,
            "vm_on": self._vm_cfg["on"],
            "vm_secs": self._vm_cfg["secs"],
        })

    def save_key(self):
        self.save_all()
        k = self.keys[self.pid]
        note = ""
        if self.pid == "nvidia" and k and not k.startswith("nvapi-"):
            note = "  (NVIDIA keys normally start with 'nvapi-' - double check it)"
        where = "saved on this PC" if self.remember_var.get() else "used for this session only"
        self.status.set(f"{PROVIDERS[self.pid]['label']} key {where}.{note}")
        self.refresh_models()

    def open_key_page(self):
        webbrowser.open(PROVIDERS[self.pid]["key_url"])

    def on_provider(self, _event=None):
        self._stash()
        label = self.prov_var.get()
        self.pid = next(p for p, c in PROVIDERS.items() if c["label"] == label)
        self.key_var.set(self.keys.get(self.pid, ""))
        vals = self.models[self.pid]
        self.model_box["values"] = vals
        self.model_var.set(self.sel.get(self.pid) or vals[0])
        self._update_labels()
        self.save_all()
        if self.pid == "nvidia" and not self.key_var.get().strip():
            self.status.set("NVIDIA NIM: paste your API key (nvapi-...) or click 'Auto-detect key on this PC'.")
        else:
            self.refresh_models()

    def autofill(self, explicit):
        """Copy detected keys into empty slots (or into every slot when the user clicked the button)."""
        notes = []
        for pid, (key, src) in self.detected.items():
            cur = self.keys.get(pid, "")
            if (explicit or cur in ("", "public")) and cur != key:
                self.keys[pid] = key
                notes.append(f"{PROVIDERS[pid]['label']}: key found in {src}")
        return notes

    def detect_now(self):
        self._stash()
        self.detected = detect_keys()
        notes = self.autofill(explicit=True)
        self.key_var.set(self.keys.get(self.pid, ""))
        self.say("\n##### Auto-detect on this PC #####\n")
        if notes:
            self.say("\n".join(notes) + "\n")
            self.status.set("API key(s) found on this PC and filled in.")
            others = [PROVIDERS[p]["label"] for p in self.detected if p != self.pid]
            if others:
                self.say("Also found a key for: " + ", ".join(others) + " - choose it in the Provider list.\n")
        else:
            self.say("No API keys found. Looked in environment variables ("
                     + ", ".join(n for c in PROVIDERS.values() for n in c["env"])
                     + ") and OpenCode's auth.json. Paste your key in the box and click 'Save key'.\n")
            self.status.set("No API key found on this PC - paste one and click 'Save key'.")
        g = detect_nvidia_gpu()
        self.say((f"NVIDIA GPU: {g}\n") if g else "NVIDIA GPU: none detected (nvidia-smi not found)\n")
        find = find_opencode()
        self.say("OpenCode CLI: " + (find if find else "not installed") + "\n")
        self.save_all()
        self.refresh_models(all_providers=True)

    def on_close(self):
        try:
            self.save_all()
            self.vm_stop.set()
            if self.vm_proc and self.vm_proc.poll() is None:
                self.vm_proc.kill()
        except Exception:
            pass
        finally:
            self.destroy()

    def _gpu_worker(self):
        g = detect_nvidia_gpu()
        self.q.put(("gpu", f"NVIDIA GPU detected on this PC: {g}" if g
                    else "NVIDIA GPU: none detected on this PC (NVIDIA cloud models still work)"))

    def refresh_models(self, all_providers=False):
        self._stash()
        pids = list(PROVIDERS) if all_providers else [self.pid]
        self.status.set("Getting models...")
        for pid in pids:
            key = self.keys.get(pid) or PROVIDERS[pid]["default_key"]
            threading.Thread(target=self._models_worker, args=(pid, key), daemon=True).start()

    def _nvidia_models_worker(self, key):
        base = PROVIDERS["nvidia"]["base"]
        if not key:
            self.q.put(("status", "NVIDIA NIM: paste your API key (nvapi-...) to load the full model list."))
            return
        ids, err = [], ""
        try:
            data = http_json(f"{base}/models", key, timeout=25)
            ids = nvidia_chat_models([m["id"] for m in data.get("data", []) if m.get("id")],
                                     dead=self.dead, good=self.good)
        except urllib.error.HTTPError as e:
            err = f"NVIDIA rejected the key (HTTP {e.code}). Check it and click 'Save key'." if e.code in (401, 403) \
                else f"NVIDIA model list error: HTTP {e.code}"
        except Exception as e:
            err = f"Could not reach NVIDIA: {e}"
        if ids:
            save_cache("nvidia", ids)
            self.q.put(("models", ("nvidia", ids)))
            self.q.put(("status", f"Loaded {len(ids)} NVIDIA chat models."))
        else:
            self.q.put(("status", (err or "NVIDIA returned no models") + " - using the saved/built-in list."))

    def _models_worker(self, pid, key):
        if pid == "nvidia":
            return self._nvidia_models_worker(key)
        ids, sources = set(), []
        try:
            data = http_json(f"{PROVIDERS['opencode']['base']}/models", key, timeout=20)
            found = {m["id"] for m in data.get("data", [])}
            if found:
                ids |= found
                sources.append("OpenCode API")
        except Exception:
            pass
        cli = find_opencode()
        if cli:
            try:
                r = subprocess.run([cli, "models"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=60, creationflags=NO_WINDOW)
                found = {ln.strip().split("/", 1)[1] for ln in r.stdout.splitlines()
                         if ln.strip().startswith("opencode/") and "/" in ln.strip()}
                if found:
                    ids |= found
                    sources.append("OpenCode CLI")
            except Exception:
                pass
        if ids:
            free_md = modelsdev_free()
            ids = sorted(i for i in ids if is_free_name(i) or i in free_md)
        if ids:
            save_cache("opencode", ids)
            self.q.put(("models", ("opencode", ids)))
            self.q.put(("status", f"Loaded {len(ids)} FREE OpenCode models from {' + '.join(sources)}."))
        else:
            ids = [i for i in load_cache("opencode") if is_free_name(i)]
            if ids:
                self.q.put(("models", ("opencode", ids)))
                self.q.put(("status", f"Offline: using {len(ids)} saved models. Check your internet connection."))
            else:
                self.q.put(("status", "Could not get the model list; you can still type a model id."))

    # ---------- install OpenCode ----------
    def install_opencode(self):
        self._stash()
        key = self.keys.get("opencode") or PROVIDERS["opencode"]["default_key"]
        if find_opencode():
            messagebox.showinfo("OpenCode", "OpenCode is already installed on this PC. Refreshing models.")
            self.refresh_models()
            return
        if not messagebox.askyesno(
            "Install OpenCode",
            "Install the OpenCode CLI on this PC now?\n\nIt uses npm (Node.js), Scoop or Chocolatey, "
            "whichever is already installed. Afterwards the app loads the OpenCode model list automatically.",
        ):
            return
        threading.Thread(target=self._install_worker, args=(key,), daemon=True).start()

    def _install_worker(self, key):
        if shutil.which("npm"):
            cmd = "npm install -g opencode-ai"
        elif shutil.which("scoop"):
            cmd = "scoop bucket add extras & scoop install extras/opencode"
        elif shutil.which("choco"):
            cmd = "choco install opencode -y"
        else:
            self.q.put(("log", "\nNo npm, Scoop or Chocolatey found. Install Node.js from https://nodejs.org "
                               "(LTS), then click 'Install OpenCode on this PC' again.\n"))
            self.q.put(("open", "https://nodejs.org"))
            return
        self._run([cmd])
        if find_opencode():
            self.q.put(("cli", "OpenCode CLI: installed"))
            self._models_worker("opencode", key)
        else:
            self.q.put(("log", "\nInstall finished but 'opencode' was not found. Close and reopen this app, or add "
                               "%APPDATA%\\npm to your PATH.\n"))

    def ask(self):
        text = self.problem.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("Describe the problem", "Type what is going wrong first.")
            return
        msg = text
        if self.sysinfo_var.get() and len(self.history) == 1:
            self.status.set("Collecting system info...")
            msg += "\n\n[System info]\n" + gather_system_info()
        self.problem.delete("1.0", "end")
        self._send(msg, text)

    def _send(self, msg, shown):
        self.history.append({"role": "user", "content": msg})
        self.ask_btn.config(state="disabled")
        self.status.set("Asking AI...")
        self.answer.insert("end", f"\n--- You ---\n{shown}\n\n--- AI ---\n")

        self.save_all()
        pid = self.pid
        base = PROVIDERS[pid]["base"]
        first = self.model_var.get().strip()
        models = [first] + [m for m in self.model_box["values"] if m != first]
        key = self.cur_key()
        hist = list(self.history)
        use_cli = pid == "opencode" and self.use_cli.get() and bool(find_opencode())

        def work():
            errors = []
            if pid == "nvidia" and not key:
                self.q.put(("error", NV_HELP))
                return
            if use_cli:
                reply = self._cli_fallback(first, hist)
                if reply:
                    self.q.put(("reply", reply))
                    return
                errors.append(f"OpenCode CLI: {self._cli_err or 'no output'}")
            for m in models[:(25 if pid == "nvidia" else 6)]:
                self.q.put(("status", f"Asking AI ({m})..."))
                try:
                    payload = {"model": m, "messages": hist, "temperature": 0.2}
                    if pid == "nvidia":
                        payload["max_tokens"] = 4096
                    res = http_json(f"{base}/chat/completions", key, payload, timeout=120)
                    msg = (res.get("choices") or [{}])[0].get("message") or {}
                    reply = THINK_RE.sub("", msg.get("content") or "").strip()
                    if not reply:
                        errors.append(f"{m}: empty reply")
                        continue
                    note = f"[answered by {m}]\n" if m != first else ""
                    if pid == "nvidia":
                        self.q.put(("good", m))
                    self.q.put(("reply", note + reply))
                    return
                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
                    errors.append(f"{m}: HTTP {e.code} {body}")
                    if pid == "nvidia" and e.code in (404, 410):
                        self.q.put(("dead", m))  # not served to this account: skip it from now on
                    if e.code in (401, 403) or "FreeUsageLimit" in body:
                        break  # key rejected / free limit is account-wide - other models will not help
                except Exception as e:
                    errors.append(f"{m}: {e}")
            if pid == "opencode" and not use_cli:
                reply = self._cli_fallback(first, hist)
                if reply:
                    self.q.put(("reply", reply))
                    return
            self.q.put(("error", "All models failed:\n- " + "\n- ".join(errors) + "\n\n"
                        + (LIMIT_HELP if pid == "opencode" else NV_HELP)))
        threading.Thread(target=work, daemon=True).start()

    def test_ai(self):
        self._stash()
        pid, key = self.pid, self.cur_key()
        models = list(self.model_box["values"])
        if pid == "nvidia":
            models = models[:40]  # catalog is large; walk the top of the list, skipping dead models
        self.status.set("Testing models...")
        threading.Thread(target=self._test_worker, args=(pid, key, models), daemon=True).start()

    def _test_worker(self, pid, key, models):
        base = PROVIDERS[pid]["base"]
        self.q.put(("log", f"\n##### Testing {PROVIDERS[pid]['label']} models (app v9) #####\n"))
        if pid == "nvidia" and not key:
            self.q.put(("log", NV_HELP + "\n"))
            self.q.put(("status", "NVIDIA NIM needs an API key - see the output log."))
            return
        working, n_ok, n_dead = None, 0, 0
        for m in models:
            if pid == "nvidia" and n_ok >= 3:
                self.q.put(("log", "Found 3 working NVIDIA models - stopping here.\n"))
                break
            try:
                res = http_json(f"{base}/chat/completions", key,
                                {"model": m, "messages": [{"role": "user", "content": "Reply with the single word OK"}],
                                 "max_tokens": 20}, timeout=60)
                msg = (res.get("choices") or [{}])[0].get("message") or {}
                txt = msg.get("content") or msg.get("reasoning_content") or ""
                ok = bool(txt.strip())
                self.q.put(("log", f"{m}: {'WORKING' if ok else 'empty reply'}\n"))
                if ok:
                    n_ok += 1
                    if not working:
                        working = m
                    if pid == "nvidia":
                        self.q.put(("good", m))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
                if pid == "nvidia" and e.code in (404, 410):
                    n_dead += 1
                    self.q.put(("dead", m))
                    self.q.put(("log", f"{m}: not available on your account (HTTP {e.code}) - removed from list\n"))
                else:
                    self.q.put(("log", f"{m}: HTTP {e.code} {body}\n"))
                    if e.code in (401, 403):
                        self.q.put(("log", "The API key was rejected - check it and click 'Save key'.\n"))
                        break
            except Exception as e:
                self.q.put(("log", f"{m}: {e}\n"))
        if pid == "nvidia" and n_dead:
            self.q.put(("log", f"Removed {n_dead} unavailable model(s) from the list.\n"))
        cli, cli_ok = (find_opencode() if pid == "opencode" else None), False
        if pid != "opencode":
            pass
        elif cli:
            m0 = working or (models[0] if models else "big-pickle")
            try:
                r = subprocess.run([cli, "run", "-m", f"opencode/{m0}", "Reply with the single word OK"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=180, creationflags=NO_WINDOW)
                out = ANSI_RE.sub("", r.stdout or "").strip()
                cli_ok = bool(out)
                detail = "WORKING" if cli_ok else "no output: " + ANSI_RE.sub("", r.stderr or "").strip()[-200:]
                self.q.put(("log", f"OpenCode CLI ({m0}): {detail}\n"))
            except Exception as e:
                self.q.put(("log", f"OpenCode CLI: {e}\n"))
        else:
            self.q.put(("log", "OpenCode CLI: not installed (use the 'Install OpenCode on this PC' button)\n"))
        if cli_ok:
            self.q.put(("usecli", True))
        if working:
            self.q.put(("setmodel", working))
            self.q.put(("status", f"Working free model selected: {working}"))
        elif cli_ok:
            self.q.put(("status", "API limit reached, but the OpenCode CLI works - 'Prefer OpenCode CLI' is on."))
        else:
            self.q.put(("status", "No model works right now - see the output log. Check your API key or try another model."))

    _cli_err = ""

    def _cli_fallback(self, model, hist):
        self._cli_err = ""
        cli = find_opencode()
        if not cli:
            self._cli_err = "not installed"
            return None
        self.q.put(("status", f"Asking OpenCode CLI ({model})..."))
        convo = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in hist)
        try:
            r = subprocess.run(
                [cli, "run", "-m", f"opencode/{model}",
                 "Follow the instructions and conversation given on stdin and answer the last user message."],
                input=convo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=300, creationflags=NO_WINDOW)
            out = ANSI_RE.sub("", r.stdout or "").strip()
            if not out:
                self._cli_err = ANSI_RE.sub("", (r.stderr or "")).strip()[-300:] or f"exit code {r.returncode}, no output"
            return out or None
        except Exception as e:
            self._cli_err = str(e)
            return None

    def handle_reply(self, reply):
        self.history.append({"role": "assistant", "content": reply})
        self.answer.insert("end", reply + "\n")
        self.answer.see("end")
        self.commands = []
        self.cmd_list.delete(0, "end")
        for block in re.findall(r"```[a-zA-Z]*\n(.*?)```", reply, re.S):
            for line in block.splitlines():
                line = line.strip()
                if line and not line.startswith(("#", "REM ", "::")):
                    self.commands.append(line)
                    self.cmd_list.insert("end", line)
        self.status.set(f"Done. {len(self.commands)} command(s) suggested. Review before running.")
        self.ask_btn.config(state="normal")

    # ---------- drive selection / repair ----------
    def refresh_drives(self):
        drives = list_drives()
        self.drive_box["values"] = drives
        if drives and not self.drive_var.get():
            sysd = [d for d in drives if d.upper().startswith(SYS_DRIVE.upper())]
            self.drive_var.set(sysd[0] if sysd else drives[0])
        # repair-loop selector: same drives, tagged with what is on them
        labelled = []
        for d in drives:
            if d[0].upper() + ":" == SYS_DRIVE.upper():
                tag = "Windows running now"
            elif has_windows(d[0]):
                tag = "Windows installation found"
            else:
                tag = "no Windows found"
            labelled.append(f"{d}  - {tag}")
        self.boot_drive_box["values"] = labelled
        cur = self.boot_drive_var.get()[:1].upper()
        keep = [x for x in labelled if cur and x[0].upper() == cur]
        sysd = [x for x in labelled if x[0].upper() + ":" == SYS_DRIVE.upper()]
        pick = keep or sysd or labelled
        self.boot_drive_var.set(pick[0] if pick else "")

    def sel_boot_letter(self):
        v = self.boot_drive_var.get().strip()
        if not v:
            messagebox.showinfo("Select drive", "No drive selected. Pick the drive that holds the Windows to fix (Windows only).")
            return None
        return v[0].upper()

    def sel_letter(self):
        v = self.drive_var.get().strip()
        if not v:
            messagebox.showinfo("Select drive", "No drive selected. Pick one from the Drive list (Windows only).")
            return None
        return v[0].upper()

    def repair(self, deep):
        L = self.sel_letter()
        if not L:
            return
        if not is_admin():
            messagebox.showwarning("Administrator needed", "Repairing a disk needs Administrator rights. Use 'Restart as Admin' first.")
            return
        flag = "/r" if deep else "/f"
        is_sys = (L + ":").upper() == SYS_DRIVE.upper()
        msg = (f"Run  chkdsk {L}: {flag}  on drive {L}:?\n\n"
               + ("This is your Windows drive, so the repair will be scheduled for the NEXT RESTART.\n\n" if is_sys
                  else "The drive will be dismounted; open files on it may be closed.\n\n")
               + ("Deep repair scans for bad sectors and can take hours.\n\n" if deep else "")
               + "Back up important files first. If the disk is physically failing, repairs can make things worse.")
        if not messagebox.askyesno("Repair drive", msg, icon="warning"):
            return
        threading.Thread(target=self._run, args=([f"echo Y| chkdsk {L}: {flag}"],), daemon=True).start()

    # ---------- virtual machine screen (QEMU) ----------
    vm_stop = threading.Event()
    vm_proc = None

    def _vm_cfg_update(self, *_):
        try:
            secs = max(20, min(900, int(self.vm_secs_var.get())))
        except Exception:
            secs = 120
        self._vm_cfg = {"on": bool(self.vm_var.get()), "secs": secs, "test": bool(self.test_var.get())}

    def _qemu_label(self):
        p = find_qemu(self.qemu_path)
        self.qemu_lbl.set("QEMU: " + (p if p else "not installed (needed for the VM screen)"))

    def install_qemu(self):
        if find_qemu(self.qemu_path):
            messagebox.showinfo("QEMU", "QEMU is already installed.")
            return
        if shutil.which("winget"):
            if messagebox.askyesno("Install QEMU",
                                   "Install QEMU (free, open-source virtual machine) with winget now?\n\n"
                                   "It is only used to boot a copy-on-write snapshot of your drive for testing."):
                threading.Thread(target=self._qemu_install_worker, daemon=True).start()
        else:
            self.say("\nwinget not found. Opening the QEMU download page - install it, then click 'Locate QEMU...' "
                     "if it is not found automatically.\n")
            webbrowser.open(QEMU_URL)

    def _qemu_install_worker(self):
        self._run(["winget install --id SoftwareFreedomConservancy.QEMU -e "
                   "--accept-source-agreements --accept-package-agreements"])
        self.q.put(("qemulbl", None))

    def locate_qemu(self):
        p = filedialog.askopenfilename(title="Pick qemu-system-x86_64.exe",
                                       filetypes=[("QEMU", "qemu-system-x86_64.exe"), ("Programs", "*.exe")])
        if p:
            self.qemu_path = p
            self.save_all()
            self._qemu_label()

    def _vm_show(self, data):
        try:
            img = tk.PhotoImage(data=data, format="ppm")
        except tk.TclError:
            return
        self.vm_img = img   # keep a reference or Tk drops the picture
        self.vm_canvas.delete("all")
        self.vm_canvas.create_image(136, 102, image=img)

    def vm_now(self):
        L = self.sel_boot_letter()
        if not L:
            return
        self.vm_btn.config(state="disabled")
        threading.Thread(target=self._vm_only, args=(L,), daemon=True).start()

    def _vm_only(self, L):
        try:
            st, det = self.vm_boot_test(L, "now", self._vm_cfg["secs"])
        except Exception as e:
            st, det = "WARN", f"VM test error: {e}"
        self.q.put(("log", f"\n[VM boot {st}] {det}\n"))
        self.q.put(("status", f"VM boot test: {st} - {det}"))
        self.q.put(("vmbtn", None))

    def save_shot(self):
        if not self.vm_img:
            messagebox.showinfo("Save picture", "No VM picture yet - run a VM test first.")
            return
        p = filedialog.asksaveasfilename(title="Save the VM picture", defaultextension=".png",
                                         initialfile="vm_screen.png", filetypes=[("PNG image", "*.png")])
        if p:
            try:
                self.vm_img.write(p, format="png")
            except Exception as e:
                messagebox.showerror("Cannot save", str(e))

    def show_shot(self, phase):
        data = self.vm_shots.get(phase)
        if data:
            self._vm_show(data)
            self.vm_cap.set("VM: last picture - " + {"before": "before fix", "after": "after fix", "now": "manual test"}.get(phase, phase))
        else:
            self.vm_cap.set("VM: no picture yet for that run")

    def vm_boot_test(self, L, phase, secs):
        """Boot the physical disk that holds drive L: in a throw-away QEMU VM, stream its screen to the VM panel and
        return (STATUS, detail).  The disk is attached as a read-only snapshot: nothing is ever written to it."""
        ph = {"before": "before fix", "after": "after fix", "now": "manual test"}.get(phase, phase)
        if os.name != "nt":
            return "SKIP", "the VM test runs on Windows only"
        qemu = find_qemu(self.qemu_path)
        if not qemu:
            return "SKIP", "QEMU not installed - click 'Install QEMU' to enable the VM screen"
        if not is_admin():
            return "SKIP", "needs Administrator (raw disk access)"
        tok = _quick(_ps(f"(Get-Partition -DriveLetter {L}).DiskNumber"), 25).split()
        num = tok[0] if tok and tok[0].isdigit() else ""
        if not num:
            return "SKIP", f"could not find the physical disk of {L}:"
        bl = _quick(["manage-bde", "-status", f"{L}:"], 25).lower()
        if "protection on" in bl or re.search(r"lock status:\s*locked", bl):
            return "SKIP", (f"BitLocker is on for {L}: - a VM has no TPM and cannot unlock it, so it would only show a "
                            "recovery-key screen (not a real problem)")
        style = _quick(_ps(f"(Get-Disk -Number {num}).PartitionStyle"), 25).strip().upper()
        uefi = style == "GPT"
        code, nvram = find_firmware(qemu)
        if uefi and not code:
            return "SKIP", "UEFI/GPT disk but QEMU's edk2-x86_64-code.fd firmware was not found in its 'share' folder"
        sk = socket.socket()
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
        sk.close()
        tmp = tempfile.mkdtemp(prefix="wfixervm")
        cmd = [qemu, "-name", "WindowsFixerVM", "-m", "2048", "-smp", "2",
               "-machine", "q35" if uefi else "pc",
               "-accel", "whpx,kernel-irqchip=off", "-accel", "tcg",     # hardware acceleration if available, else slow software
               "-vga", "std", "-display", "none", "-nic", "none", "-no-reboot", "-rtc", "base=localtime",
               "-qmp", f"tcp:127.0.0.1:{port},server,nowait",
               "-device", "ich9-ahci,id=ahci",
               "-drive", "file=" + "\\\\.\\PhysicalDrive" + num + ",format=raw,if=none,id=d0,snapshot=on",
               "-device", "ide-hd,drive=d0,bus=ahci.0,bootindex=0"]
        if uefi:
            if nvram:
                vars_copy = os.path.join(tmp, "vars.fd")
                shutil.copyfile(nvram, vars_copy)
                cmd += ["-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
                        "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}"]
            else:
                cmd += ["-bios", code]
        self.vm_stop.clear()
        self.q.put(("vmcap", f"VM ({ph}): starting {L}: ..."))
        self.q.put(("log", f"\n##### VM boot test ({ph}) of {L}: = PhysicalDrive{num}, "
                           f"{'UEFI' if uefi else 'BIOS'}, read-only snapshot #####\n"))
        errf = open(os.path.join(tmp, "qemu_err.txt"), "wb")
        sock = rf = proc = None
        stats, last_small = [], None
        timeline, keyshots, prev_raw, prev_t = [], [], None, 0
        t0 = time.time()
        try:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf, creationflags=NO_WINDOW)
            except Exception as e:
                return "FAIL", f"could not start QEMU: {e}"
            self.vm_proc = proc
            while time.time() - t0 < 20 and proc.poll() is None:        # connect to QEMU's monitor
                try:
                    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
                    rf = sock.makefile("rb")
                    rf.readline()
                    qmp_cmd(sock, rf, "qmp_capabilities")
                    break
                except Exception:
                    if sock:
                        sock.close()
                    sock = rf = None
                    time.sleep(0.5)
            last_change, n = time.time(), 0
            while sock and proc.poll() is None and not self.vm_stop.is_set() and time.time() - t0 < secs:
                fn = os.path.join(tmp, f"f{n % 2}.ppm")
                n += 1
                try:
                    qmp_cmd(sock, rf, "screendump", {"filename": fn})
                    with open(fn, "rb") as f:
                        data = f.read()
                    res = ppm_analyze(data)
                except Exception:
                    res = None
                if res:
                    st, small = res
                    if stats and ppm_changed(stats[-1]["sig"], st["sig"]):
                        last_change = time.time()
                        if prev_raw is not None and stats[-1]["nonblack"] >= 0.02:   # the screen that was just replaced
                            keyshots = (keyshots + [(int(prev_t), prev_raw)])[-3:]
                    stats.append(st)
                    last_small = small
                    prev_raw, prev_t = data, time.time() - t0
                    timeline.append((int(prev_t), "black" if st["nonblack"] < 0.02 else ("blue" if st["blue"] > 0.5 else "picture")))
                    self.q.put(("vmframe", (small, f"VM ({ph}): {int(time.time() - t0)}s of {secs}s")))
                time.sleep(1)
            exited = proc.poll() is not None
            stopped = self.vm_stop.is_set()
            elapsed = int(time.time() - t0)
            vm_shutdown(proc, sock, rf)
            errf.close()
            try:
                with open(os.path.join(tmp, "qemu_err.txt"), "r", errors="replace") as f:
                    err = " ".join(f.read().split())[-220:]
            except Exception:
                err = ""
            if last_small:
                self.q.put(("vmshot", (phase, last_small)))
            self.q.put(("vmcap", f"VM ({ph}): finished after {elapsed}s"))
            end = " (stopped early)" if stopped else ""
            last = stats[-1] if stats else None
            if exited and proc.returncode not in (0, None):
                result = ("FAIL", "VM could not run: " + (err or f"QEMU exit code {proc.returncode}"))
            elif exited:
                result = ("WARN", f"VM restarted or shut down by itself after {elapsed}s - possible crash or reboot loop")
            elif not stats:
                result = ("WARN", "VM started but no screen picture could be captured" + (f" ({err})" if err else ""))
            elif max(s["nonblack"] for s in stats) < 0.02:
                result = ("FAIL", "black screen the whole time - nothing bootable on the drive (or it cannot start in a VM)" + end)
            elif last["blue"] > 0.5:
                result = ("WARN", "blue screen at the end - Automatic Repair / recovery / crash screen? Look at the VM screen" + end)
            elif time.time() - last_change > 45 and last["nonblack"] < 0.15:
                result = ("WARN", "screen frozen and mostly dark - maybe stuck at startup" + end)
            else:
                result = ("OK", f"shows a picture after {elapsed}s - look at the VM screen to confirm it reached the desktop/login" + end)
            # read the text on the screens (Windows OCR) so the AI knows what the user would see
            shots = keyshots + ([(int(prev_t), prev_raw)] if prev_raw is not None else [])
            if shots:
                self.q.put(("vmcap", f"VM ({ph}): reading the screen text..."))
            texts = ocr_screens(tmp, shots) if shots else []
            report = vm_report_text(L, ph, uefi, secs, elapsed, exited, result, timeline, texts)
            self.vm_report[phase] = report
            self.q.put(("log", "\n" + report + "\n"))
            tail = texts[-1][1] if texts and not texts[-1][1].startswith("(") else ""
            if tail:
                result = (result[0], result[1] + " | screen text: " + tail[:90])
            self.q.put(("vmcap", f"VM ({ph}): finished after {elapsed}s"))
            return result
        finally:
            try:
                if proc and proc.poll() is None:
                    proc.kill()
                errf.close()
            except Exception:
                pass
            self.vm_proc = None
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------- Auto-test screen ----------
    def test_now(self):
        L = self.sel_boot_letter()
        if not L:
            return
        self.test_btn.config(state="disabled")
        threading.Thread(target=self._run_tests, args=(L, "before"), daemon=True).start()

    def _run_tests(self, L, phase):
        """Run the read-only auto tests on the OS on drive L: (worker thread) and show them in the test screen."""
        when = "before" if phase == "before" else "after"
        self.q.put(("teststate", f"Testing {L}: ({when} fix)..."))
        try:
            os_label, rows = quick_tests(L, is_admin())
        except Exception as e:
            os_label, rows = "unknown", [("Auto test", "WARN", f"test could not run: {e}")]
        cfg = self._vm_cfg
        if cfg["on"]:
            if os_label.startswith("no operating system"):
                rows.append(("VM boot (visual hint)", "SKIP", "no operating system found on the drive"))
            else:
                self.q.put(("teststate", f"VM boot test of {L}: ({when} fix) - watch the VM screen..."))
                try:
                    vst, vdet = self.vm_boot_test(L, phase, cfg["secs"])
                except Exception as e:
                    vst, vdet = "WARN", f"VM test error: {e}"
                rows.append(("VM boot (visual hint)", vst, vdet))
        self.q.put(("testrows", (phase, L, os_label, rows)))
        self.q.put(("log", f"\n##### Auto test ({when} fix) of {L}: - {os_label} #####\n"
                    + "\n".join(f"  [{st:<4}] {name}: {det}" for name, st, det in rows) + "\n"))
        self.q.put(("testdone", None))
        return rows

    def _vm_first(self, L):
        """VM FIRST: run the auto tests + VM boot check of drive L: BEFORE the diagnostics/fixes, so the AI knows the real symptom."""
        self.vm_report.pop("before", None)
        self.pre_before = None
        if not (self._vm_cfg["test"] and self._vm_cfg["on"]):
            return
        self.q.put(("status", f"VM first: booting drive {L}: to see the problem before fixing..."))
        self.q.put(("log", f"\n##### VM FIRST: checking what the PC does at startup (drive {L}:) before any diagnostics or fixes #####\n"))
        rows = self._run_tests(L, "before")
        self.pre_before = (L, rows)

    def _show_tests(self, phase, L, os_label, rows):
        t = self.test_tree
        if phase == "before":
            t.delete(*t.get_children())
            self.test_counts = {"before": None, "after": None}
        for name, st, det in rows:
            if phase == "before":
                t.insert("", "end", iid=name, text=name, values=(st, "", det), tags=(st.lower(),))
            elif t.exists(name):
                t.item(name, values=(t.set(name, "before"), st, det), tags=(st.lower(),))
            else:
                t.insert("", "end", iid=name, text=name, values=("-", st, det), tags=(st.lower(),))
        self.test_counts[phase] = sum(1 for r in rows if r[1] in ("WARN", "FAIL"))
        c = self.test_counts
        self.test_state_var.set(f"Target: {L}:  -  {os_label}")
        self.test_sum_var.set(f"Issues: {c['before']} before" + ("" if c["after"] is None else f"  ->  {c['after']} after"))

    # ---------- Automatic Repair loop ----------
    def boot_fix(self):
        if not is_admin():
            messagebox.showwarning("Administrator needed",
                                   "Fixing a boot / repair loop needs Administrator rights. Use 'Restart as Admin' first.")
            return
        L = self.sel_boot_letter()
        if not L:
            return
        offline = (L + ":").upper() != SYS_DRIVE.upper()   # Windows on ANOTHER drive, not the one running this app
        if offline and not has_windows(L):
            messagebox.showerror(
                "No Windows found",
                f"No Windows installation was found on {L}:.\n\nPick another drive. If the drive is protected by BitLocker, "
                f"unlock it first (Administrator Command Prompt):\n  manage-bde -unlock {L}: -RecoveryPassword <48-digit key>\n"
                "then click 'Refresh drives'.")
            return
        if offline:
            ok = messagebox.askyesno(
                f"Fix Automatic Repair loop on drive {L}: with AI",
                f"The Windows on drive {L}: is NOT the Windows running now - it is repaired OFFLINE.\n\n"
                f"Step 1: read-only diagnostics from {L}: (Startup Repair log, update/servicing logs, pending updates, "
                "crash dumps, its event log, BitLocker and disk health, chkdsk scan).\n"
                f"Step 2: the AI runs offline fixes automatically, for drive {L}: ONLY, up to {AUTOFIX_MAX_ROUNDS} rounds - for example "
                f"DISM /Image:{L}:\\ /RevertPendingActions, offline SFC, chkdsk {L}: /f and renaming pending.xml.\n\n"
                "Anything that would change the Windows running on this PC, or any other drive, is blocked, as are formatting, "
                "repartitioning and deleting data. Afterwards put the drive back in its PC and try to boot.\n\n"
                "Back up important files first. Continue?",
                icon="warning")
        else:
            ok = messagebox.askyesno(
                "Fix Automatic Repair loop with AI",
                "Step 1: collect read-only boot diagnostics (Startup Repair log, boot config, WinRE status, pending updates, "
                f"update history, error events, health of system drive {L}:).\n"
                f"Step 2: the AI runs fix commands automatically (up to {AUTOFIX_MAX_ROUNDS} rounds) - for example "
                "DISM /RevertPendingActions, DISM /RestoreHealth, SFC, chkdsk, reagentc /enable and a few safe bcdedit settings "
                "(your boot config is backed up first).\n\n"
                "Formatting, repartitioning, deleting data and other bcdedit changes are blocked. "
                "Most fixes need a RESTART afterwards.\n\nContinue?",
                icon="warning")
        if not ok:
            return
        for b in (self.boot_btn, self.disk_btn, self.diskfix_btn, self.ask_btn, self.fix_btn, self.test_btn):
            b.config(state="disabled")
        self._full_buttons("disabled")
        self.autofix_stop = False
        self.stop_btn.config(state="normal")
        threading.Thread(target=self._disk_worker, args=(L, False, True, offline), daemon=True).start()

    def analyze_log(self):
        path = filedialog.askopenfilename(
            title="Pick SrtTrail.txt or repair_log.txt",
            filetypes=[("Text / log files", "*.txt *.log"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as f:
                raw = f.read(400000)
            text = (raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
                    else raw.decode("utf-8-sig", "replace"))
        except Exception as e:
            messagebox.showerror("Cannot read file", str(e))
            return
        if len(text) > 12000:
            text = text[:6000] + "\n...[trimmed]...\n" + text[-6000:]
        prompt = ("Here is a Windows startup repair / boot log (" + os.path.basename(path) + "). Explain why Windows is stuck "
                  "in the Automatic Repair loop and give the safest fix steps, either from the Windows recovery Command "
                  "Prompt or from inside Windows.\n\n" + text)
        self._send(prompt, f"[Boot log '{os.path.basename(path)}' sent to AI]")

    def make_winre_script(self):
        initial = ""
        if os.name == "nt":
            try:
                mask = ctypes.windll.kernel32.GetLogicalDrives()
                for i in range(26):
                    if mask & (1 << i) and ctypes.windll.kernel32.GetDriveTypeW(f"{chr(65 + i)}:\\") == 2:
                        initial = f"{chr(65 + i)}:\\"
                        break
            except Exception:
                pass
        path = filedialog.asksaveasfilename(
            title="Save the repair script (best: on a USB stick)", initialdir=initial or None,
            initialfile="repair_boot_loop.bat", defaultextension=".bat", filetypes=[("Batch file", "*.bat")])
        if not path:
            return
        try:
            with open(path, "w", encoding="ascii", newline="\r\n") as f:
                f.write(WINRE_SCRIPT.strip("\n") + "\n")
        except Exception as e:
            messagebox.showerror("Cannot save", str(e))
            return
        steps = (
            "HOW TO USE (when Windows will not start):\n"
            "1. Put this .bat on a USB stick. On a working PC you can make a Windows install USB with Microsoft's "
            "'Media Creation Tool'.\n"
            "2. Boot the broken PC from the Windows USB (or let it fail 2-3 times to reach 'Recovery'). Choose "
            "Repair your computer > Troubleshoot > Advanced options > Command Prompt.\n"
            "3. Find the USB letter:  diskpart  >  list volume  >  exit.  Then run, e.g.:   E:\\repair_boot_loop.bat\n"
            "4. If Windows is protected by BitLocker, unlock first:  manage-bde -unlock C: -RecoveryPassword <48-digit key>\n"
            "5. When it finishes: type  exit  and choose Continue. If it still loops, open the SrtTrail.txt it copied next to "
            "the .bat and use 'Analyze a boot log with AI...' in this app on a working PC.\n")
        self.say(f"\n##### Recovery-screen repair script saved: {path} #####\n{steps}")
        messagebox.showinfo("Script saved", f"Saved:\n{path}\n\n{steps}")

    # ---------- AI fixes the selected drive ----------
    def auto_fix_disk(self):
        L = self.sel_letter()
        if not L:
            return
        if not is_admin():
            messagebox.showwarning("Administrator needed",
                                   "Checking and repairing a drive needs Administrator rights. Use 'Restart as Admin' first.")
            return
        if not messagebox.askyesno(
            "Auto-fix selected drive with AI",
            f"Step 1: run read-only health checks on drive {L}: (health, SMART, errors, CHKDSK scan - can take several minutes).\n"
            f"Step 2: the AI reads the results and RUNS repair commands automatically, for drive {L}: ONLY "
            f"(for example Repair-Volume or chkdsk /f), up to {AUTOFIX_MAX_ROUNDS} rounds.\n\n"
            "Formatting, repartitioning and deleting data are blocked. "
            + ("This is your Windows drive, so chkdsk repairs are scheduled for the NEXT RESTART.\n\n"
               if (L + ":").upper() == SYS_DRIVE.upper() else "The drive may be dismounted during repair.\n\n")
            + "Back up important files first - if the disk is physically failing, repairs can make things worse.",
            icon="warning",
        ):
            return
        self.disk_btn.config(state="disabled")
        self.diskfix_btn.config(state="disabled")
        self.ask_btn.config(state="disabled")
        self.fix_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self._full_buttons("disabled")
        self.autofix_stop = False
        self.stop_btn.config(state="normal")
        threading.Thread(target=self._disk_worker, args=(L, True), daemon=True).start()

    # ---------- auto disk check ----------
    def auto_disk(self):
        L = self.sel_letter()
        if not L:
            return
        if not messagebox.askyesno(
            "Auto check hard disk",
            "Run read-only disk checks (health, SMART, volumes, disk errors, CHKDSK scan) "
            "and send the results to the AI?\n\nNothing is changed on your disk. "
            "CHKDSK can take several minutes."
            + ("" if is_admin() else "\n\nNot running as Administrator: SMART, dirty-bit and CHKDSK checks will be skipped."),
        ):
            return
        self.disk_btn.config(state="disabled")
        self.ask_btn.config(state="disabled")
        threading.Thread(target=self._disk_worker, args=(L,), daemon=True).start()

    def _disk_worker(self, L, fix=False, boot=False, offline=False):
        report, admin = [], is_admin()
        if fix or boot:
            self._vm_first(L)
            if self.autofix_stop:
                self.q.put(("ans", "\n=== Stopped before the fix started. ===\n"))
                self.q.put(("autodone", None))
                return
        devnum = ""
        try:
            r = subprocess.run(_ps(f"(Get-Partition -DriveLetter {L}).DiskNumber"), capture_output=True, text=True,
                               encoding=ENC, errors="replace", timeout=30, creationflags=NO_WINDOW)
            tok = r.stdout.strip().split()[0] if r.stdout.strip() else ""
            devnum = tok if tok.isdigit() else ""
        except Exception:
            pass
        self.q.put(("log", f"\n##### Checking drive {L}: (physical disk number: {devnum or 'unknown'}) #####\n"))
        tokens = {"@@L@@": f"{L}:", "@@V@@": f"-DriveLetter {L} ",
                  "@@D@@": f"-DeviceNumber {devnum} " if devnum else ""}
        if offline:
            checks = OFFLINE_CHECKS + DISK_CHECKS
        else:
            checks = (BOOT_CHECKS if boot else []) + [c for c in DISK_CHECKS
                                                      if not (boot and c[0].startswith("CHKDSK"))]
        for label, argv, needs_admin in checks:
            for k, v in tokens.items():
                argv = [a.replace(k, v) for a in argv]
            self.q.put(("log", f"\n=== {label} ===\n"))
            if needs_admin and not admin:
                self.q.put(("log", "Skipped (needs Administrator).\n"))
                report.append(f"### {label}\nSkipped: app not running as Administrator")
                continue
            self.q.put(("status", f"Checking: {label}..."))
            try:
                r = subprocess.run(argv, capture_output=True, text=True, encoding=ENC,
                                   errors="replace", timeout=1500, creationflags=NO_WINDOW)
                out = ((r.stdout or "") + (r.stderr or "")).strip() or "(no output)"
            except Exception as e:
                out = f"[error] {e}"
            self.q.put(("log", out + "\n"))
            report.append(f"### {label}\n{out[:3000]}")
        prompt = (
            "Here are automatic read-only hard disk checks for drive " + L + ": on my Windows PC. Analyze them: is the disk "
            "healthy? List any problems (bad sectors, SMART or health warnings, high wear/temperature, "
            "NTFS/disk errors, dirty volume, low free space), how urgent each is, and what I should do. "
            "If data may be at risk, tell me to back up first. Only suggest safe commands.\n\n"
            + "\n\n".join(report)
        )
        if boot and offline:
            prompt = (
                "A Windows PC keeps going into the 'Preparing Automatic Repair' / 'Diagnosing your PC' loop. Its drive is attached "
                "to THIS working PC as drive " + L + ": (this is NOT the Windows running now - it must be repaired offline). "
                "Here are read-only diagnostics read from drive " + L + ":. Find the most likely cause of the loop and fix it "
                "on drive " + L + ": only.\n\n" + "\n\n".join(report)
            )
            self.q.put(("autofixboot", (L, prompt, True)))
        elif boot:
            prompt = (
                "My Windows PC keeps going into the 'Preparing Automatic Repair' / 'Diagnosing your PC' loop (Windows only "
                "starts sometimes, or after the loop). Here are automatic read-only diagnostics from the running system, "
                "including the system drive " + L + ":. Find the most likely cause of the loop and fix it.\n\n"
                + "\n\n".join(report)
            )
            self.q.put(("autofixboot", (L, prompt, False)))
        elif fix:
            self.q.put(("autofixdisk", (L, prompt)))
        else:
            self.q.put(("autoask", prompt))

    # ---------- FULL PC SCAN (+ auto-fix everything) ----------
    def _full_buttons(self, state):
        for b in (self.fullscan_btn, self.fullfix_btn):
            b.config(state=state)

    def _fill_quick(self, _event=None, *_):
        cat = self.qcat_var.get()
        search_query = getattr(self, "qsearch_var", None)
        query = search_query.get().strip().lower() if search_query else ""
        groups = QUICK_FIX_GROUPS if cat not in QUICK_FIX_GROUPS else {cat: QUICK_FIX_GROUPS[cat]}
        self.quick.delete(0, "end")
        for grp in groups.values():
            for name in grp:
                if not query or query in name.lower():
                    self.quick.insert("end", name)

    def full_scan(self, fix):
        admin = is_admin()
        if fix:
            ok = messagebox.askyesno(
                "Scan whole PC + auto-fix everything",
                "Step 1: create a system restore point (if ticked) so you can roll back.\n"
                "Step 2: scan the WHOLE PC read-only: system files, disk, network, Windows Update, services, drivers, "
                "error logs, crashes, Defender, startup and more (about 1-3 minutes).\n"
                f"Step 3: the AI reads the scan and RUNS fix commands automatically, up to {FULL_MAX_ROUNDS} rounds.\n\n"
                "Formatting, repartitioning, deleting files (except temp files), registry deletes and restarts are blocked. "
                "Click Stop at any time. Back up important files first."
                + ("" if admin else "\n\nNOT running as Administrator: most checks and fixes will be skipped or fail. "
                                    "Click 'Restart as Admin' first for best results.")
                + "\n\nContinue?",
                icon="warning")
        else:
            ok = messagebox.askyesno(
                "Scan whole PC",
                "Run a read-only scan of the whole PC and send the results to the AI for an explanation?\n\n"
                "Nothing is changed (except a restore point, if ticked)."
                + ("" if admin else "\n\nNot running as Administrator: some checks will be skipped."))
        if not ok:
            return
        restore = bool(self.restore_var.get())
        self.autofix_stop = False
        self._full_buttons("disabled")
        for b in (self.fix_btn, self.diskfix_btn, self.boot_btn, self.disk_btn, self.test_btn):
            b.config(state="disabled")
        self.stop_btn.config(state="normal")
        threading.Thread(target=self._fullscan_worker, args=(fix, restore), daemon=True).start()

    def _fullscan_worker(self, fix, restore):
        admin = is_admin()
        if fix:
            self._vm_first(SYS_DRIVE[0].upper())
            if self.autofix_stop:
                self.q.put(("ans", "\n=== Stopped before the fix started. ===\n"))
                self.q.put(("autodone", None))
                return
        if restore:
            if admin:
                self.q.put(("log", "\n##### Creating a system restore point (you can roll back from Windows 'Create a restore point') #####\n"))
                self.q.put(("status", "Creating restore point..."))
                out = _quick(_ps(RESTORE_PS), 180)
                self.q.put(("log", (out or "Restore point command finished.") + "\n"
                            "(Windows only makes one restore point per 24 hours - a warning above about that is normal.)\n"))
            else:
                self.q.put(("log", "\nRestore point skipped (needs Administrator).\n"))
        self.q.put(("log", "\n##### Full PC scan (read-only) #####\n"))
        report = []

        def run_one(c):
            return None if (c[2] and not admin) else (_quick(c[1], 300) or "(no output)")

        def emit(c, out):
            self.q.put(("log", f"\n=== {c[0]} ===\n"))
            if out is None:
                self.q.put(("log", "Skipped (needs Administrator).\n"))
                report.append(f"### {c[0]}\nSkipped: app not running as Administrator")
            else:
                self.q.put(("log", out[:3000] + "\n"))
                report.append(f"### {c[0]}\n{out[:1500]}")

        first = [c for c in FULL_CHECKS if c[0].startswith("CPU")]   # measured alone, so the other checks don't skew it
        rest = [c for c in FULL_CHECKS if c not in first]
        for c in first:
            self.q.put(("status", f"Full scan: {c[0]}..."))
            emit(c, run_one(c))
        ex = ThreadPoolExecutor(max_workers=4)
        try:
            for i, (c, out) in enumerate(zip(rest, ex.map(run_one, rest)), 1):
                if self.autofix_stop:
                    break
                self.q.put(("status", f"Full scan {i}/{len(rest)}: {c[0]}"))
                emit(c, out)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)
        if self.autofix_stop:
            self.q.put(("ans", "\n=== Full scan stopped. ===\n"))
            self.q.put(("status", "Full scan stopped"))
            self.q.put(("autodone", None))
            return
        body = "\n\n".join(report)
        if fix:
            prompt = ("My Windows PC has problems and I do not know exactly which. Below is a full read-only scan of the whole PC "
                      "(running as Administrator: " + ("yes" if admin else "NO") + "). Find every real problem and fix them.\n\n" + body)
            self.q.put(("autofixfull", prompt))
        else:
            prompt = ("Below is a full read-only scan of my whole Windows PC. Analyze it: list every REAL problem (ignore harmless "
                      "noise), most serious first, say how urgent each is, and give the safest fix for each with the exact commands "
                      "(one per line in ```cmd blocks). If hardware looks like it is failing, tell me to back up first.\n\n" + body)
            self.q.put(("fullask", prompt))

    # ---------- AUTO-FIX: AI picks commands, app runs them, AI sees the output, repeat ----------
    autofix_stop = False
    _proc = None

    def autofix_stop_now(self):
        self.autofix_stop = True
        self.vm_stop.set()
        self.status.set("Stopping auto-fix...")
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        except Exception:
            pass

    def autofix(self):
        text = self.problem.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("Describe the problem", "Type what is going wrong first, then click Auto-fix.")
            return
        warn = "" if is_admin() else ("\n\nNot running as Administrator: many fixes (SFC, DISM, services, network reset) "
                                      "will fail. Click 'Restart as Admin' first for best results.")
        if not messagebox.askyesno(
            "Auto-fix with AI",
            "The AI will choose Windows commands and this app will RUN THEM AUTOMATICALLY on this PC, "
            f"up to {AUTOFIX_MAX_ROUNDS} rounds, sending the output back to the AI each time.\n\n"
            "Destructive commands (format, diskpart, deleting files/registry, shutdown/restart) are blocked. "
            "You can click Stop at any time. Back up important files first." + warn + "\n\nContinue?",
            icon="warning",
        ):
            return
        msg = text
        if self.sysinfo_var.get() and len(self.history) == 1:
            self.status.set("Collecting system info...")
            msg += "\n\n[System info]\n" + gather_system_info()
        self.problem.delete("1.0", "end")
        self._start_autofix(msg, text)

    def _start_autofix(self, msg, shown, drive=None, boot=False, offline=False, full=False):
        """Start the auto-fix loop. If drive is a letter, repair commands are limited to that drive."""
        self.save_all()
        pid = self.pid
        first = self.model_var.get().strip()
        cfg = (pid, PROVIDERS[pid]["base"], self.cur_key(), first,
               [first] + [m for m in self.model_box["values"] if m != first],
               pid == "opencode" and self.use_cli.get() and bool(find_opencode()))
        self.autofix_stop = False
        self.ask_btn.config(state="disabled")
        self.fix_btn.config(state="disabled")
        self.disk_btn.config(state="disabled")
        self.diskfix_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.boot_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self._full_buttons("disabled")
        auto_test = bool(self.test_var.get())   # read here: Tk variables belong to the main thread
        threading.Thread(target=self._autofix_worker,
                         args=(msg, shown, cfg, list(self.history), drive, boot, offline, auto_test, full),
                         daemon=True).start()

    def _chat_sync(self, hist, cfg):
        """One blocking AI call with model fallback. Returns the reply text or raises RuntimeError."""
        pid, base, key, first, models, use_cli = cfg
        errors = []
        if pid == "nvidia" and not key:
            raise RuntimeError(NV_HELP)
        if use_cli:
            reply = self._cli_fallback(first, hist)
            if reply:
                return reply
            errors.append(f"OpenCode CLI: {self._cli_err or 'no output'}")
        for m in models[:(25 if pid == "nvidia" else 6)]:
            if self.autofix_stop:
                raise RuntimeError("Stopped.")
            self.q.put(("status", f"Asking AI ({m})..."))
            try:
                payload = {"model": m, "messages": hist, "temperature": 0.2}
                if pid == "nvidia":
                    payload["max_tokens"] = 4096
                res = http_json(f"{base}/chat/completions", key, payload, timeout=120)
                msg = (res.get("choices") or [{}])[0].get("message") or {}
                reply = THINK_RE.sub("", msg.get("content") or "").strip()
                if reply:
                    if pid == "nvidia":
                        self.q.put(("good", m))
                    return reply
                errors.append(f"{m}: empty reply")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
                errors.append(f"{m}: HTTP {e.code} {body}")
                if pid == "nvidia" and e.code in (404, 410):
                    self.q.put(("dead", m))
                if e.code in (401, 403) or "FreeUsageLimit" in body:
                    break
            except Exception as e:
                errors.append(f"{m}: {e}")
        if pid == "opencode" and not use_cli:
            reply = self._cli_fallback(first, hist)
            if reply:
                return reply
        raise RuntimeError("All models failed:\n- " + "\n- ".join(errors) + "\n\n"
                           + (LIMIT_HELP if pid == "opencode" else NV_HELP))

    def _run_capture(self, cmd, timeout=1800):
        """Run one command non-interactively, stream output to the log, return (output, exit_code)."""
        self.q.put(("log", f"\n> {cmd}\n"))
        self.q.put(("status", f"Running: {cmd}"))
        buf, code = [], -1
        try:
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, encoding=ENC, errors="replace", creationflags=NO_WINDOW)
            self._proc = p
            timer = threading.Timer(timeout, p.kill)
            timer.start()
            for line in p.stdout:
                buf.append(line)
                self.q.put(("log", line))
            p.wait()
            timer.cancel()
            code = p.returncode
        except Exception as e:
            buf.append(f"[error] {e}")
        out = "".join(buf).strip() or "(no output)"
        if len(out) > 2400:
            out = out[:1200] + "\n...[trimmed]...\n" + out[-1200:]
        self.q.put(("log", f"[exit code {code}]\n"))
        return out, code

    def _autofix_worker(self, msg, shown, cfg, hist, drive=None, boot=False, offline=False, auto_test=False, full=False):
        admin = "yes" if is_admin() else "NO (commands needing admin will fail)"
        max_rounds = FULL_MAX_ROUNDS if full else AUTOFIX_MAX_ROUNDS
        general = not drive and not offline and not boot   # typed-problem or full-PC mode: give the AI the fix toolbox
        hist.append({"role": "user", "content": msg + "\n\n" + AUTOFIX_RULES.format(admin=admin)
                     + (DISK_RULES.format(L=drive) if drive else "")
                     + (OFFLINE_RULES.format(L=drive) if offline else (BOOT_RULES if boot else ""))
                     + (FULL_RULES if full else "") + (TOOLBOX if general else "")})
        # offline mode: remember whether a scratch folder already existed so we only clean up our own
        pre_scratch = bool(offline and drive and os.path.isdir(f"{drive}:\\scratch"))
        # auto test: snapshot of the OS BEFORE any fix command runs (the diagnostics before this point are read-only)
        test_L = drive or SYS_DRIVE[0].upper()
        pre, self.pre_before = self.pre_before, None
        if pre and pre[0] == test_L:
            before_rows = pre[1]          # the VM check already ran first, before the diagnostics
        else:
            self.vm_report.pop("before", None)
            before_rows = self._run_tests(test_L, "before") if auto_test else None
        vm_obs = self.vm_report.pop("before", "")
        if before_rows:
            hist[-1]["content"] += ("\n\n[Auto test of " + test_L + ": before the fix - the 'VM boot' row is a visual hint from "
                                    "booting the drive in a VM]\n"
                                    + "\n".join(f"{n}: {st} - {d}" for n, st, d in before_rows)
                                    + (("\n\n" + vm_obs + VM_RULES) if vm_obs else ""))
        self.q.put(("ans", f"\n--- You (Auto-fix) ---\n{shown}\n"))
        outcome = "Auto-fix stopped."
        for rnd in range(1, max_rounds + 1):
            if self.autofix_stop:
                break
            self.q.put(("status", f"Auto-fix round {rnd}/{max_rounds}: asking AI..."))
            try:
                reply = self._chat_sync(hist, cfg)
            except Exception as e:
                self.q.put(("ans", f"[Error] {e}\n"))
                outcome = "Auto-fix stopped: AI error."
                break
            hist.append({"role": "assistant", "content": reply})
            self.q.put(("ans", f"\n--- AI (round {rnd}) ---\n{reply}\n"))
            cmds = extract_cmds(reply)
            self.q.put(("cmds", cmds))
            if not cmds:
                outcome = ("Auto-fix finished: problem reported FIXED." if "FIX_DONE" in reply
                           else "Auto-fix finished - the AI needs you to do something manually (see answer).")
                break
            results = []
            for c in cmds[:5]:
                if self.autofix_stop:
                    break
                if cmd_blocked(c, boot, offline):
                    self.q.put(("log", f"\n> {c}\n[BLOCKED in auto-fix mode - run it yourself if you really want it]\n"))
                    results.append(f"$ {c}\nBLOCKED: not allowed in auto-fix mode. Use a safer alternative or tell the user.")
                    continue
                if offline and not offline_ok(c, drive):
                    self.q.put(("log", f"\n> {c}\n[BLOCKED: offline-repair mode only allows commands that act on {drive}: itself - "
                                       "nothing that touches the running Windows or another drive]\n"))
                    results.append(f"$ {c}\nBLOCKED: offline mode. Use only offline forms that target {drive}: (for example "
                                   f"DISM /Image:{drive}:\\ ..., sfc /offbootdir={drive}:\\ /offwindir={drive}:\\Windows ..., "
                                   f"bcdedit /store {drive}:\\Boot\\BCD ...), or tell the user.")
                    continue
                if drive and not drive_ok(c, drive):
                    self.q.put(("log", f"\n> {c}\n[BLOCKED: drive-repair mode only allows disk commands that name {drive}: and no other drive]\n"))
                    results.append(f"$ {c}\nBLOCKED: disk commands must explicitly target drive {drive}: and no other drive.")
                    continue
                out, code = self._run_capture(c)
                note = "" if code == 0 or is_admin() else "\n(Failed and the app is NOT running as Administrator.)"
                results.append(f"$ {c}\n[exit code {code}]\n{out}{note}")
            if self.autofix_stop:
                break
            hist.append({"role": "user", "content":
                         "Command results:\n\n" + "\n\n".join(results) +
                         "\n\nContinue. If fixed, reply with FIX_DONE (no code block). Otherwise give the next commands."})
            if rnd == max_rounds:
                outcome = f"Auto-fix reached the {max_rounds}-round limit. Check the answer/log, then click Auto-fix again to continue."
        if offline and drive and not pre_scratch:   # remove the DISM scratch folder this run created on the target drive
            shutil.rmtree(f"{drive}:\\scratch", ignore_errors=True)
        if auto_test and not self.autofix_stop:   # test again and show what the fix changed
            self.q.put(("status", "Auto test: checking the OS again after the fix..."))
            after_rows = self._run_tests(test_L, "after")
            nb = sum(1 for r in (before_rows or []) if r[1] in ("WARN", "FAIL"))
            na = sum(1 for r in after_rows if r[1] in ("WARN", "FAIL"))
            self.q.put(("ans", f"\nAuto test of {test_L}: {nb} issue(s) before the fix -> {na} issue(s) after.\n"))
        self.q.put(("autosync", hist))
        self.q.put(("ans", f"\n=== {outcome} ===\n"))
        self.q.put(("status", outcome))
        self.q.put(("autodone", None))

    # ---------- running commands ----------
    def run_quick(self):
        sel = self.quick.curselection()
        if not sel:
            return
        self.confirm_and_run([QUICK_FIXES[self.quick.get(i)] for i in sel])

    def run_selected(self):
        sel = self.cmd_list.curselection()
        if not sel:
            messagebox.showinfo("Select", "Select one or more suggested commands first.")
            return
        self.confirm_and_run([self.commands[i] for i in sel])

    def copy_answer(self):
        try:
            content = self.answer.get("1.0", "end").strip()
            if content:
                self.clipboard_clear()
                self.clipboard_append(content)
                self.status.set("AI diagnostic plan copied to clipboard.")
        except Exception:
            pass

    def export_report(self):
        try:
            problem_text = self.problem.get("1.0", "end").strip() or "General system diagnostic inquiry"
            ai_text = self.answer.get("1.0", "end").strip()
            if not ai_text:
                messagebox.showinfo("Export Report", "Run a diagnostic scan or query the AI first before exporting a report.")
                return

            dt = time.strftime("%Y-%m-%d %H:%M:%S")
            file_dt = time.strftime("%Y%m%d_%H%M%S")
            sys_info = gather_system_info()
            log_text = self.log.get("1.0", "end").strip()
            hostname = socket.gethostname()
            admin_status = "Administrator" if is_admin() else "Standard User"

            html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Windows Fixer AI - Diagnostic & Repair Report</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0a0e17; color: #f8fafc; margin: 0; padding: 24px; line-height: 1.5; }}
  .container {{ max-width: 920px; margin: 0 auto; }}
  .header {{ border-bottom: 2px solid #22304d; padding-bottom: 16px; margin-bottom: 24px; }}
  h1 {{ color: #38bdf8; margin: 0 0 6px 0; font-size: 24px; }}
  .meta {{ color: #94a3b8; font-size: 13px; }}
  .card {{ background: #131b2e; border: 1px solid #22304d; border-radius: 8px; padding: 18px; margin-bottom: 18px; }}
  h2 {{ color: #38bdf8; font-size: 15px; margin-top: 0; border-bottom: 1px solid #22304d; padding-bottom: 8px; }}
  pre {{ background: #0c1322; color: #38bdf8; border: 1px solid #22304d; border-radius: 6px; padding: 12px; overflow-x: auto; font-family: Consolas, monospace; font-size: 13px; white-space: pre-wrap; }}
  .log-box {{ color: #94a3b8; font-size: 12px; max-height: 350px; overflow-y: auto; }}
  .badge {{ display: inline-block; padding: 3px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; }}
  .badge-admin {{ background: #064e3b; color: #34d399; }}
  .footer {{ text-align: center; color: #64748b; font-size: 12px; margin-top: 30px; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🛠 Windows Fixer AI - Diagnostic & Repair Report</h1>
    <div class="meta">Generated: {dt} • Host: {hostname} • Privilege: <span class="badge badge-admin">{admin_status}</span></div>
  </div>
  <div class="card">
    <h2>1. Reported Issue / Symptoms</h2>
    <p>{problem_text}</p>
  </div>
  <div class="card">
    <h2>2. System Hardware & OS Specifications</h2>
    <pre>{sys_info}</pre>
  </div>
  <div class="card">
    <h2>3. AI Diagnostic Analysis & Repair Plan</h2>
    <pre>{ai_text}</pre>
  </div>
  <div class="card">
    <h2>4. Execution & Verification Log</h2>
    <pre class="log-box">{log_text if log_text else 'No execution logs recorded.'}</pre>
  </div>
  <div class="footer">
    Generated by Windows Fixer AI • Open Source Diagnostic Suite
  </div>
</div>
</body>
</html>"""
            default_name = f"windows_fixer_report_{file_dt}.html"
            p = filedialog.asksaveasfilename(title="Save Diagnostic Report", initialfile=default_name,
                                             defaultextension=".html", filetypes=[("HTML Report", "*.html")])
            if p:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(html_content)
                self.status.set(f"Diagnostic report saved: {os.path.basename(p)}")
                if messagebox.askyesno("Report Saved", f"Diagnostic report saved successfully:\n{p}\n\nOpen report in your web browser now?"):
                    webbrowser.open("file://" + os.path.abspath(p))
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    def create_restore_point_now(self):
        if not is_admin():
            messagebox.showwarning("Administrator Needed",
                                   "Creating a system restore point requires Administrator privileges. Click 'Restart as Admin' first.")
            return
        if not messagebox.askyesno("Create Restore Point", "Create a Windows System Restore Point now so you can roll back at any time?"):
            return
        self.status.set("Creating system restore point...")
        def work():
            self.q.put(("log", "\n##### Creating System Restore Point #####\n"))
            out = _quick(_ps(RESTORE_PS), 180)
            self.q.put(("log", (out or "Restore point command finished.") + "\n"))
            self.q.put(("status", "Restore point created successfully."))
        threading.Thread(target=work, daemon=True).start()

    def confirm_and_run(self, cmds):
        if not cmds:
            return
        risky = [c for c in cmds if any(re.search(p, c, re.I) for p in RISKY)]

        # Custom Dark Mode Modal Dialog
        dlg = tk.Toplevel(self)
        dlg.title("Confirm Execution" if not risky else "⚠️ Confirm Risky Commands")
        dlg.geometry("680x500")
        dlg.configure(bg=THEME["bg_root"])
        dlg.transient(self)
        dlg.grab_set()

        try:
            x = self.winfo_rootx() + (self.winfo_width() - 680) // 2
            y = self.winfo_rooty() + (self.winfo_height() - 500) // 2
            dlg.geometry(f"+{max(20, x)}+{max(20, y)}")
        except Exception:
            pass

        hdr_bg = "#7f1d1d" if risky else THEME["bg_header"]
        hdr_fg = "#fca5a5" if risky else THEME["accent_cyan"]
        hdr_title = "⚠️ ATTENTION: POTENTIALLY SENSITIVE OPERATIONS" if risky else "🛠 REVIEW & EXECUTE COMMANDS"
        hdr_sub = ("These commands modify critical system states or disk configurations. Review each line before executing:"
                   if risky else
                   f"Review the {len(cmds)} command(s) below before executing sequentially in elevated prompt:")

        hdr_frame = tk.Frame(dlg, bg=hdr_bg, padx=14, pady=10)
        hdr_frame.pack(fill="x")
        tk.Label(hdr_frame, text=hdr_title, font=("Segoe UI", 10, "bold"), bg=hdr_bg, fg=hdr_fg).pack(anchor="w")
        tk.Label(hdr_frame, text=hdr_sub, font=("Segoe UI", 8), bg=hdr_bg, fg="#f8fafc", justify="left").pack(anchor="w", pady=(2, 0))

        content_frame = ttk.Frame(dlg, style="Root.TFrame", padding=12)
        content_frame.pack(fill="both", expand=True)

        txt_box = ScrolledText(content_frame, wrap="none", font=("Consolas", 10),
                               bg=THEME["bg_input"], fg=THEME["fg_text"],
                               insertbackground=THEME["accent_cyan"], relief="solid", bd=1,
                               highlightthickness=1, highlightbackground=THEME["border"])
        txt_box.pack(fill="both", expand=True)

        for idx, c in enumerate(cmds, 1):
            is_c_risky = any(re.search(p, c, re.I) for p in RISKY)
            prefix = f"{idx:2d}. [RISKY] " if is_c_risky else f"{idx:2d}. "
            txt_box.insert("end", prefix + c + "\n")
        txt_box.config(state="disabled")

        btn_bar = ttk.Frame(dlg, style="Root.TFrame", padding=12)
        btn_bar.pack(fill="x")

        def copy_cmds():
            try:
                self.clipboard_clear()
                self.clipboard_append("\n".join(cmds))
                self.status.set(f"Copied {len(cmds)} command(s) to clipboard.")
            except Exception:
                pass

        ttk.Button(btn_bar, text="📋 Copy Commands", command=copy_cmds, style="TButton").pack(side="left")

        def do_execute():
            dlg.destroy()
            threading.Thread(target=self._run, args=(cmds,), daemon=True).start()

        run_style = "Danger.TButton" if risky else "Success.TButton"
        run_txt = "⚠️ Run Risky Commands Anyway" if risky else f"▶ Execute {len(cmds)} Command(s)"

        ttk.Button(btn_bar, text="Cancel", command=dlg.destroy, style="TButton").pack(side="right", padx=(6, 0))
        ttk.Button(btn_bar, text=run_txt, command=do_execute, style=run_style).pack(side="right")

    def _run(self, cmds):
        self.autofix_stop = False
        for c in cmds:
            if getattr(self, "autofix_stop", False):
                self.q.put(("log", "\n[Command execution cancelled by user]\n"))
                break
            self.q.put(("log", f"\n> {c}\n"))
            self.q.put(("status", f"Running: {c}"))
            try:
                p = subprocess.Popen(
                    c, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    encoding="oem" if os.name == "nt" else "utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self._proc = p
                for line in p.stdout:
                    self.q.put(("log", line))
                p.wait()
                self.q.put(("log", f"[exit code {p.returncode}]\n"))
                if p.returncode != 0 and not is_admin():
                    self.q.put(("log", "Tip: this may need Administrator rights (Restart as Admin).\n"))
            except Exception as e:
                self.q.put(("log", f"[error] {e}\n"))
            finally:
                self._proc = None
        self.q.put(("status", "Finished running commands"))

    # ---------- queue pump ----------
    def _pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "models":
                    pid, ids = val
                    self.models[pid] = ids
                    if pid == self.pid:
                        self.model_box["values"] = ids
                        if self.model_var.get() not in ids:
                            self.model_var.set(ids[0])
                elif kind in ("dead", "good"):
                    lst = [m for m in self.models["nvidia"] if m != val]
                    if kind == "dead":
                        self.dead.add(val)
                        if val in self.good:
                            self.good.remove(val)
                    else:
                        self.dead.discard(val)
                        if val in self.good:
                            self.good.remove(val)
                        self.good.insert(0, val)
                        lst.insert(0, val)
                    self.models["nvidia"] = lst or list(PROVIDERS["nvidia"]["fallback"])
                    save_cache("nvidia", self.models["nvidia"])
                    if self.pid == "nvidia":
                        self.model_box["values"] = self.models["nvidia"]
                        if kind == "dead" and self.model_var.get() == val:
                            self.model_var.set(self.models["nvidia"][0])
                        elif kind == "good" and self.model_var.get() not in self.models["nvidia"]:
                            self.model_var.set(val)
                    self.save_all()
                elif kind == "ans":
                    self.answer.insert("end", val)
                    self.answer.see("end")
                elif kind == "cmds":
                    self.commands = list(val)
                    self.cmd_list.delete(0, "end")
                    for c in self.commands:
                        self.cmd_list.insert("end", c)
                elif kind == "autosync":
                    self.history = val
                elif kind == "autofixdisk":
                    L, prompt = val
                    self._start_autofix(prompt, f"[Auto-fix drive {L}: - health check results sent to AI, see output log]", drive=L)
                elif kind == "autofixfull":
                    self._start_autofix(val, "[Full PC scan: results of the whole-PC scan sent to AI - see output log. The AI now fixes the problems it found.]", full=True)
                elif kind == "fullask":
                    self._full_buttons("normal")
                    for b in (self.fix_btn, self.diskfix_btn, self.boot_btn, self.disk_btn, self.test_btn):
                        b.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self._send(val, "[Full PC scan results sent to AI - see output log below]")
                elif kind == "autofixboot":
                    L, prompt, offline = val
                    where = f"on drive {L}: (offline)" if offline else "(running Windows)"
                    self._start_autofix(prompt, f"[Auto-fix Automatic Repair loop {where} - boot diagnostics sent to AI, see output log]",
                                        drive=L, boot=True, offline=offline)
                elif kind == "vmframe":
                    self._vm_show(val[0])
                    self.vm_cap.set(val[1])
                elif kind == "vmcap":
                    self.vm_cap.set(val)
                elif kind == "vmshot":
                    self.vm_shots[val[0]] = val[1]
                elif kind == "vmbtn":
                    self.vm_btn.config(state="normal")
                elif kind == "qemulbl":
                    self._qemu_label()
                elif kind == "testrows":
                    self._show_tests(*val)
                elif kind == "teststate":
                    self.test_state_var.set(val)
                elif kind == "testdone":
                    if str(self.stop_btn["state"]) == "disabled":   # not while an auto-fix is running
                        self.test_btn.config(state="normal")
                elif kind == "autodone":
                    self.test_btn.config(state="normal")
                    self.boot_btn.config(state="normal")
                    self.disk_btn.config(state="normal")
                    self.diskfix_btn.config(state="normal")
                    self.ask_btn.config(state="normal")
                    self.fix_btn.config(state="normal")
                    self._full_buttons("normal")
                    self.stop_btn.config(state="disabled")
                elif kind == "gpu":
                    self.gpu_var.set(val)
                    if hasattr(self, "hdr_gpu_lbl"):
                        short = val.replace("NVIDIA GPU detected on this PC: ", "").replace("NVIDIA GPU: none detected on this PC (NVIDIA cloud models still work)", "Standard Display / CPU")
                        self.hdr_gpu_lbl.config(text=f"GPU: {short[:28]}")
                elif kind == "reply":
                    self.handle_reply(val)
                elif kind == "error":
                    self.answer.insert("end", f"[Error] {val}\n")
                    self.status.set("Error talking to the AI")
                    self.ask_btn.config(state="normal")
                elif kind == "usecli":
                    self.use_cli.set(True)
                elif kind == "setmodel":
                    self.model_var.set(val)
                elif kind == "cli":
                    self.cli_var.set(val)
                elif kind == "open":
                    webbrowser.open(val)
                elif kind == "autoask":
                    self.disk_btn.config(state="normal")
                    self._send(val, "[Auto hard disk check results for the selected drive sent to AI - see output log below]")
                elif kind == "log":
                    self.say(val)
                elif kind == "status":
                    self.status.set(val)
        except queue.Empty:
            pass
        self.after(100, self._pump)


if __name__ == "__main__":
    App().mainloop()
