"""
telegram_bot/config.py
Configuración del bot de Telegram
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Directorio base del proyecto
BASE_DIR = Path(__file__).resolve().parent.parent
TELEGRAM_BOT_DIR = BASE_DIR / 'telegram_bot'
REPORTS_DIR = BASE_DIR / 'reports'
TEMP_DIR = TELEGRAM_BOT_DIR / 'temp_files'

# Crear directorio de archivos temporales si no existe
TEMP_DIR.mkdir(exist_ok=True)

# Token y configuración de Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRA_BOT_TOKEN', '')
if not TELEGRAM_BOT_TOKEN:
    raise ValueError('TELEGRA_BOT_TOKEN no configurado en .env')

# Configuración del servidor FastAPI (para obtener datos)
SERVER_URL = os.getenv('SERVER_URL', 'http://localhost:8000')

# Límites de Telegram
MAX_MESSAGE_LENGTH = 4096  # Límite de caracteres por mensaje
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB máximo para descargar archivos

# Emojis y símbolos para formato
EMOJIS = {
    'loading': '⏳',
    'success': '✅',
    'error': '❌',
    'stop': '⛔',
    'chart': '📊',
    'table': '📋',
    'download': '📥',
    'file': '📄',
    'report': '📃',
    'graph': '📈',
    'search': '🔍',
    'info': 'ℹ️',
    'warning': '⚠️',
    'check': '✓',
    'cross': '✗',
}
