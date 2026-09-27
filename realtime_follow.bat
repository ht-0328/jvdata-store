@echo off
goto :main
rem ===========================================================================
rem  jvdata-store - 開催日の間ずっと動き、各レースの発走前に速報を取り直す。
rem
rem    realtime_follow.bat                       : 今日の開催日を追う。
rem    realtime_follow.bat --date 2026-10-03     : 後ろに付けた引数は jvstore realtime --follow に渡る。
rem
rem  段取り（src/jvstore/realtime_follow.py）:
rem    1. はじめに、その日の速報を全部取る（出馬表・馬体重・全賭式のオッズ …）。
rem    2. 各レースの発走の12分前に、そのレースの全賭式のオッズと、その日の馬体重・開催情報を取り直す。
rem    3. 最後のレースの発走から20分たったら、成績と払戻を取って終わる。
rem  人が見ていなくても動くように、終わっても pause しない（Windows のタスク スケジューラから呼ばれる）。
rem  keiba-yosou の tools/フォワードテスト/ が、この取り直しのあとの断面で予想して記録する。
rem
rem  このファイルの決まり:
rem    - 日本語は、上の goto :main と下の :main の間（このコメントの中）にだけ書く。
rem      cmd.exe は bat をコンソールのコードページ（日本語 Windows では 932）で読むので、
rem      実行される行に UTF-8 の日本語があると、行が途中で切れて誤動作する。
rem    - 文字コードは UTF-8（BOM なし）、改行は CRLF で保存する。
rem ===========================================================================
:main

cd /d "%~dp0"

where uv >nul 2>&1
if not errorlevel 1 set "UV=uv"
if not errorlevel 1 goto :run

set "UV=%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe"
if exist "%UV%" goto :run

echo [ERROR] uv was not found. Install uv or put it on PATH.
exit /b 1

:run
"%UV%" sync --quiet
if errorlevel 1 exit /b 1

set PYTHONIOENCODING=utf-8
"%UV%" run jvstore realtime --follow --db jvdata.duckdb %*
exit /b %errorlevel%
