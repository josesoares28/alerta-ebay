@echo off
cd /d "%~dp0"
echo Pausando a verificacao na nuvem (GitHub) e no PC...
gh workflow disable alerta.yml -R josesoares28/alerta-ebay
schtasks /Delete /TN "Alerta eBay" /F 2>nul
echo Pausado. Para voltar, rode LIGAR.bat.
pause
