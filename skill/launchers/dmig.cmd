@echo off
rem Run the migration tool without installing it: dmig <command> <repository> [options]
rem Uses %DMIG_PYTHON% if set, else "py -3", else "python".
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%scripts;%PYTHONPATH%"
if defined DMIG_PYTHON (
  "%DMIG_PYTHON%" -m dmig %*
) else (
  where py >nul 2>nul && (py -3 -m dmig %*) || (python -m dmig %*)
)
exit /b %ERRORLEVEL%
