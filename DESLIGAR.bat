@echo off
cd /d "%~dp0"
echo Pausando a verificacao na nuvem (GitHub) e no PC...
python nuvem.py desligar
schtasks /Delete /TN "Alerta eBay" /F 2>nul
echo Para voltar, rode LIGAR.bat.
pause
