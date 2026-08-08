"""
telegram_bot/api_client.py
Cliente para comunicarse con el servidor FastAPI
"""
import requests
import json
from typing import Dict, Any, Optional, List
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class APIClient:
    """Cliente para comunicarse con el servidor FastAPI del pipeline"""
    
    def __init__(self, server_url: str):
        self.server_url = server_url
        self.session = requests.Session()
        self.timeout = 30
    
    def enviar_consulta(self, session_id: str, pregunta: str) -> Dict[str, Any]:
        """Envía una consulta al servidor y obtiene la respuesta"""
        try:
            response = self.session.post(
                f"{self.server_url}/chat",
                json={
                    "session_id": session_id,
                    "pregunta": pregunta,
                    "origen": "telegram"
                },
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            return {
                "error": "La consulta tardó demasiado. Intenta con una pregunta más específica.",
                "success": False
            }
        except requests.exceptions.ConnectionError:
            return {
                "error": "No se pudo conectar con el servidor. Por favor, intenta más tarde.",
                "success": False
            }
        except requests.exceptions.HTTPError as e:
            return {
                "error": f"Error del servidor: {e.response.status_code}",
                "success": False
            }
        except Exception as e:
            logger.error(f"Error en consulta: {str(e)}")
            return {
                "error": f"Error inesperado: {str(e)}",
                "success": False
            }
    
    def descargar_archivo(self, ruta_relativa: str, directorio_destino: Path) -> Optional[Path]:
        """Descarga un archivo del servidor"""
        try:
            # Construir URL del archivo
            url = f"{self.server_url}/files/{ruta_relativa}"
            
            response = self.session.get(url, timeout=self.timeout, stream=True)
            response.raise_for_status()
            
            # Crear nombre de archivo
            nombre_archivo = Path(ruta_relativa).name
            ruta_destino = directorio_destino / nombre_archivo
            
            # Descargar archivo
            with open(ruta_destino, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            return ruta_destino
        except Exception as e:
            logger.error(f"Error descargando archivo: {str(e)}")
            return None
    
    def reset_sesion(self, session_id: str) -> bool:
        """Llama a POST /reset/{session_id} para borrar el historial del servidor."""
        try:
            response = self.session.post(
                f"{self.server_url}/reset/{session_id}",
                timeout=10,
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Error reseteando sesión en servidor: {e}")
            return False

    def verificar_servidor(self) -> bool:
        """Verifica si el servidor está disponible"""
        try:
            response = self.session.get(
                f"{self.server_url}/health",
                timeout=5
            )
            return response.status_code == 200
        except:
            return False
    
    def obtener_sesiones_activas(self) -> Dict[str, Any]:
        """Obtiene información de sesiones activas (debug)"""
        try:
            response = self.session.get(
                f"{self.server_url}/sessions",
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except:
            return {"sesiones": 0, "error": "No disponible"}
