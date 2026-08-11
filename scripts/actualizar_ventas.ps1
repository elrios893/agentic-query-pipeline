# Corre la ingesta incremental de ventas_2026 y, solo si trajo filas nuevas,
# refresca la vista materializada ventas_unificada.
# Pensado para correr varias veces por la manana via Task Scheduler (el TXT
# de red se genera ~7:30pm del dia anterior, esto es una red de seguridad
# por si el primer intento del dia encuentra el archivo bloqueado o la red
# caida -- la ingesta es idempotente, correrla de mas no duplica datos).

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo 'deploy\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("actualizar_ventas_{0}.log" -f (Get-Date -Format 'yyyyMMdd'))

function Log($msg) {
    $line = "[{0:HH:mm:ss}] {1}" -f (Get-Date), $msg
    Write-Output $line
    Add-Content -Path $logFile -Value $line
}

Log "===== Inicio actualizacion ventas 2026 ====="

$ingestaOutput = & python src\ingesta_postgres.py --2026 2>&1
$ingestaExit = $LASTEXITCODE
$ingestaOutput | Add-Content -Path $logFile
$ingestaOutput | Write-Output

if ($ingestaExit -ne 0) {
    Log "ERROR: ingesta_postgres.py --2026 fallo (exit $ingestaExit). No se refresca la vista."
    exit 1
}

$sinNuevas = $ingestaOutput | Select-String -Pattern 'Insertadas \(nuevas\)\s*:\s*0\s*$'
if ($sinNuevas) {
    Log "Sin filas nuevas -- se omite el refresco de la vista (ya esta al dia)."
    Log "===== Fin (sin cambios) ====="
    exit 0
}

Log "Filas nuevas detectadas -- refrescando ventas_unificada..."
$refreshOutput = & python scripts\refrescar_vista_ventas_items.py 2>&1
$refreshExit = $LASTEXITCODE
$refreshOutput | Add-Content -Path $logFile
$refreshOutput | Write-Output

if ($refreshExit -ne 0) {
    Log "ERROR: refrescar_vista_ventas_items.py fallo (exit $refreshExit)."
    exit 1
}

Log "===== Fin (actualizado) ====="
exit 0
