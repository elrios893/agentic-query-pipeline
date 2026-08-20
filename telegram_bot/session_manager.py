"""
telegram_bot/session_manager.py
Gestiona las sesiones de usuarios de Telegram
"""
import threading
import time
from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime

# TTL de sesión — mismo valor que TTL_SESION_SEGUNDOS en src/session_store.py.
# El agente está pensado para consultas puntuales, no sesiones largas: sin
# esto, una sesión de Telegram vivía en memoria para siempre (solo se borraba
# con /reset o al reiniciar el proceso del bot). Subido temporalmente de 10 a
# 60 min para pruebas con usuarios reales — considerar volver a bajarlo para
# uso normal en producción.
TTL_SESION_SEGUNDOS = 3600  # 60 minutos sin actividad → sesión huérfana

@dataclass
class TelegramSession:
    """Representa una sesión de usuario en Telegram"""
    user_id: int
    chat_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_message_at: datetime = field(default_factory=datetime.now)
    turno: int = 0
    mensaje_anterior: Optional[str] = None
    contexto: Dict = field(default_factory=dict)  # Contexto para refinamientos
    archivos_generados: list = field(default_factory=list)  # Archivos creados en esta sesión

class SessionManager:
    """Gestor centralizado de sesiones de Telegram"""

    def __init__(self):
        self.sesiones: Dict[int, TelegramSession] = {}
        self._lock = threading.Lock()
        # Hilo de limpieza TTL — corre más seguido que el TTL para que una
        # sesión inactiva no quede viva de más esperando el próximo barrido.
        self._limpiar_hilo = threading.Thread(
            target=self._ciclo_limpieza_ttl,
            daemon=True,
        )
        self._limpiar_hilo.start()

    def obtener_o_crear(self, user_id: int, chat_id: int, username: Optional[str] = None,
                        first_name: Optional[str] = None) -> TelegramSession:
        """Obtiene o crea una sesión para un usuario"""
        with self._lock:
            if user_id not in self.sesiones:
                self.sesiones[user_id] = TelegramSession(
                    user_id=user_id,
                    chat_id=chat_id,
                    username=username,
                    first_name=first_name
                )
            else:
                # Actualizar datos si cambian
                sesion = self.sesiones[user_id]
                if chat_id != sesion.chat_id:
                    sesion.chat_id = chat_id
                if username:
                    sesion.username = username
                if first_name:
                    sesion.first_name = first_name

            self.sesiones[user_id].last_message_at = datetime.now()
            return self.sesiones[user_id]

    def obtener(self, user_id: int) -> Optional[TelegramSession]:
        """Obtiene una sesión existente"""
        return self.sesiones.get(user_id)

    def eliminar(self, user_id: int):
        """Elimina una sesión"""
        with self._lock:
            self.sesiones.pop(user_id, None)

    def _ciclo_limpieza_ttl(self):
        """Limpia sesiones de Telegram huérfanas (sin actividad hace más de
        TTL_SESION_SEGUNDOS). Corre en un hilo daemon, igual que la limpieza
        TTL del servidor en src/session_store.py."""
        while True:
            time.sleep(120)  # 2 minutos
            ahora = datetime.now()
            with self._lock:
                expiradas = [
                    uid for uid, ses in self.sesiones.items()
                    if (ahora - ses.last_message_at).total_seconds() > TTL_SESION_SEGUNDOS
                ]
                for uid in expiradas:
                    del self.sesiones[uid]
            if expiradas:
                print(f'[SessionManager] Limpieza TTL: {len(expiradas)} sesión(es) de Telegram eliminada(s).')
    
    def incrementar_turno(self, user_id: int):
        """Incrementa el contador de turnos"""
        sesion = self.obtener_o_crear(user_id, 0)
        sesion.turno += 1
        return sesion.turno
    
    def agregar_archivo(self, user_id: int, ruta_archivo: str):
        """Agrega un archivo generado a la sesión"""
        sesion = self.obtener(user_id)
        if sesion and ruta_archivo not in sesion.archivos_generados:
            sesion.archivos_generados.append(ruta_archivo)
    
    def obtener_archivos(self, user_id: int) -> list:
        """Obtiene los archivos generados en la sesión"""
        sesion = self.obtener(user_id)
        return sesion.archivos_generados if sesion else []
    
    def resetear(self, user_id: int):
        """Resetea el estado conversacional de una sesión (turno, historial, archivos)
        pero conserva los datos de identidad del usuario."""
        sesion = self.sesiones.get(user_id)
        if sesion:
            sesion.turno = 0
            sesion.mensaje_anterior = None
            sesion.contexto = {}
            sesion.archivos_generados = []

    def limpiar_archivos(self, user_id: int):
        """Limpia los archivos generados"""
        sesion = self.obtener(user_id)
        if sesion:
            sesion.archivos_generados = []
    
    def resumen_sesiones(self) -> str:
        """Devuelve un resumen de sesiones activas"""
        total = len(self.sesiones)
        if total == 0:
            return "No hay sesiones activas"
        
        resumen = f"Sesiones activas: {total}\n"
        for user_id, sesion in list(self.sesiones.items())[:10]:  # Mostrar solo las 10 últimas
            nombre = sesion.first_name or sesion.username or f"User {user_id}"
            resumen += f"  • {nombre} (Turno {sesion.turno})\n"
        
        if total > 10:
            resumen += f"  ... y {total - 10} más"
        
        return resumen

# Instancia global del gestor de sesiones
session_manager = SessionManager()
