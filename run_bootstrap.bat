@echo off
setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
set "PORTABLE_DIR=%ROOT%python"
set "PY=%PORTABLE_DIR%\python.exe"

REM --- Release'ten indirilecek portable Python + GNU Radio ortamı ---
set "BOOTSTRAP_URL=https://github.com/hub10com/PlutoOFDMTransceiver/releases/download/v1.0.0/python.zip"
set "BOOTSTRAP_ZIP=%ROOT%python.zip"

REM Eğer portable Python mevcut değilse indir
if not exist "%PY%" (
  echo [!] Portable Python bulunamadı. Indiriliyor...
  powershell -NoLogo -NoProfile -ExecutionPolicy Bypass ^
    -Command "Invoke-WebRequest -Uri '%BOOTSTRAP_URL%' -OutFile '%BOOTSTRAP_ZIP%'"
  if errorlevel 1 (
    echo [X] Indirme basarisiz. Internet baglantisini veya URL'yi kontrol edin.
    exit /b 1
  )
  echo [*] Arsiv aciliyor...
  powershell -NoLogo -NoProfile -ExecutionPolicy Bypass ^
    -Command "Expand-Archive -Path '%BOOTSTRAP_ZIP%' -DestinationPath '%ROOT%' -Force"
  del /q "%BOOTSTRAP_ZIP%" >nul 2>&1
)

REM PATH ayarlari
set "PATH=%ROOT%python;%ROOT%python\Scripts;%ROOT%python\Library\bin;%ROOT%scripts;%PATH%"

REM Ana uygulamayi baslat
pushd "%ROOT%"
"%PY%" -u "%ROOT%main.py"
popd
endlocal
