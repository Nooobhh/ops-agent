@echo off
REM 挂载 workbuddy 用户级 skills 指向本仓 skills 目录(单一真源)
REM 前置:workbuddy 用户级旧 ops skill 已删除(手动或 rmdir)
REM 用法:在工作机 D:\hh-work\project\ops-agent\ 下执行
REM 需管理员权限运行 cmd

setlocal
set REPO=%CD%
set USER_SKILLS=C:\Users\86529\.workbuddy\skills

echo REPO=%REPO%
echo USER_SKILLS=%USER_SKILLS%

for %%s in (ops-triage ops-deep-dive kb-write) do (
    if exist "%USER_SKILLS%\%%s" (
        echo 已存在旧 skill %USER_SKILLS%\%%s,请手动 rmdir 后重跑
        goto :abort
    )
    mklink /J "%USER_SKILLS%\%%s" "%REPO%\skills\%%s"
    if errorlevel 1 (
        echo mklink 失败:%%s
        goto :abort
    )
)

echo junction 挂载完成:
for %%s in (ops-triage ops-deep-dive kb-write) do (
    dir "%USER_SKILLS%\%%s" | findstr "JUNCTION"
)
goto :eof

:abort
echo 中止,清理后重试
exit /b 1
