@echo off
REM Recovery build: free the esbuild daemon lock, reinstall deps, build.
echo Killing leftover esbuild service processes (build daemons)...
taskkill /f /im esbuild.exe 2>nul
cd /d F:\AI-Interview-Model-F-V2\frontend\admin-dashboard
echo Installing dependencies (npm install)...
call npm install --no-audit --no-fund
echo Building admin dashboard...
call npm run build
echo Exit code: %ERRORLEVEL%
timeout /t 8
