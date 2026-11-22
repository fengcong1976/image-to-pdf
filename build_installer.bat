@echo off
rem Build exe + installer in one go.
rem   dist\<app name>\<app name>.exe              portable folder
rem   dist\安装包\<app name>-<version>-安装包.exe  per-user installer, no UAC
rem Requires Inno Setup 6 (ISCC.exe) installed.
setlocal
cd /d "%~dp0"

call build_exe.bat
if errorlevel 1 exit /b 1

set ISCC=
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set ISCC=C:\Program Files\Inno Setup 6\ISCC.exe
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if "%ISCC%"=="" (
  echo.
  echo [FAILED] ISCC.exe not found. Install Inno Setup 6 first:
  echo          https://jrsoftware.org/isdl.php
  exit /b 1
)

"%ISCC%" installer.iss
if errorlevel 1 (
  echo.
  echo [FAILED] Installer compile failed, see the log above.
  exit /b 1
)

echo.
echo [OK] Installer built. See the dist folder.
endlocal
