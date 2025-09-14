@echo off
setlocal enableextensions
REM --- App root (bu .bat'ın bulunduğu klasör)
set "ROOT=%~dp0"

REM --- Portable Radioconda/Python kökü:  %ROOT%\python\
set "PY=%ROOT%python\python.exe"

REM --- PATH'e portable env'in kritik klasörlerini ekle
set "PATH=%ROOT%python;%ROOT%python\Scripts;%ROOT%python\Library\bin;%PATH%"

REM (İsteğe bağlı) GNU Radio'nun PyQt/Qt plugin araması için ek klasörler gerekiyorsa buraya eklersin

pushd "%ROOT%"
"%PY%" -u "%ROOT%main.py"
popd
