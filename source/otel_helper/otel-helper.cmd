@echo off
REM ABOUTME: Unified otel-helper entry point for Windows. Tries the fast Go binary first;
REM ABOUTME: if AV blocks it, falls back to the PowerShell script seamlessly.
REM
REM Claude Code invokes this via otelHeadersHelper. Output is JSON on stdout.
setlocal
"%~dp0otel-helper.exe" %* 2>nul && exit /b 0
REM Windows PowerShell must not inherit a PowerShell 7 PSModulePath (Claude Code started
REM from a pwsh terminal); clearing it restores the Windows PowerShell module defaults.
set "PSModulePath="
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0otel-helper.ps1" %*
