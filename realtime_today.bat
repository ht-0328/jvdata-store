@echo off
goto :main
rem ===========================================================================
rem  jvdata-store - 当日の速報をダブルクリックでまとめて取り込む。
rem
rem    ダブルクリック                       : 今日の速報を取り込む。
rem    realtime_today.bat --date 2026-09-27 : 後ろに付けた引数は jvstore realtime に渡る
rem                                           （別の開催日や、--db で別の DB を指定できる）。
rem
rem  取り込むもの（src/jvstore/realtime.py）:
rem    出馬表・取消・騎手変更、天候と馬場状態、馬体重、マイニング予想、
rem    その日の中央の全レースの全賭式のオッズ（単勝〜3連単）。
rem  馬体重は発走の約1時間前に出る。オッズは発走まで動くので、予想の直前に取り直す。
rem  取り込みのあと、keiba-yosou の tools/当日の予想/run.bat で予想する。
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
echo         Looked for: %UV%
goto :failed

:run
"%UV%" sync --quiet
if errorlevel 1 (
    echo [ERROR] "uv sync" failed.
    goto :failed
)

set PYTHONIOENCODING=utf-8
"%UV%" run jvstore realtime --db jvdata.duckdb %*
if errorlevel 1 goto :failed
echo.
echo Done. Next: run the same-day prediction bat in keiba-yosou.
pause
exit /b 0

:failed
echo.
pause
exit /b 1
