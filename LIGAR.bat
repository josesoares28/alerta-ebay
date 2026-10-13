@echo off
cd /d "%~dp0"
gh workflow enable alerta.yml -R josesoares28/alerta-ebay
echo Verificacao na nuvem ligada (a cada 30 min).
pause
