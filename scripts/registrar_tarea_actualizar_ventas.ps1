# Registra la tarea programada que corre scripts\actualizar_ventas.ps1
# a las 6:00, 7:00, 8:00 y 9:00am todos los dias.
# Ejecutar UNA VEZ en el Windows Server, como Administrador.

$repo   = 'C:\agentic-query-pipeline'
$script = Join-Path $repo 'scripts\actualizar_ventas.ps1'
$nombreTarea = 'ActualizarVentas2026'

$accion = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""

$horarios = @('06:00', '07:00', '08:00', '09:00')
$triggers = $horarios | ForEach-Object {
    New-ScheduledTaskTrigger -Daily -At $_
}

# IgnoreNew: si una corrida todavia esta activa cuando llega el siguiente
# horario, no lanza una segunda instancia en paralelo.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -DontStopOnIdleEnd

Register-ScheduledTask -TaskName $nombreTarea `
    -Action $accion `
    -Trigger $triggers `
    -Settings $settings `
    -User 'SYSTEM' `
    -RunLevel Highest `
    -Description 'Ingesta incremental de ventas_2026 + refresco de ventas_unificada (corre 4x en la manana como red de seguridad).' `
    -Force

Write-Output "Tarea '$nombreTarea' registrada con horarios: $($horarios -join ', ')"
