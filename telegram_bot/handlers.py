"""
telegram_bot/handlers.py
Manejadores de eventos del bot de Telegram
"""
import logging
import time
from typing import Optional
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from telegram_bot.config import TELEGRAM_BOT_TOKEN, SERVER_URL, EMOJIS, TEMP_DIR
from telegram_bot.session_manager import session_manager
from telegram_bot.api_client import APIClient
from telegram_bot.formatters import MessageFormatter

logger = logging.getLogger(__name__)

# Cliente API global
api_client = APIClient(SERVER_URL)

class TelegramHandlers:
    """Manejadores de eventos del bot"""
    
    @staticmethod
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /start — inicia la sesión del usuario"""
        user = update.effective_user
        chat_id = update.effective_chat.id
        
        # Crear o actualizar sesión
        sesion = session_manager.obtener_o_crear(
            user_id=user.id,
            chat_id=chat_id,
            username=user.username,
            first_name=user.first_name
        )
        
        mensaje = f"""
{EMOJIS['info']} *¡Bienvenido al Consultor de Ventas Creytex!*

Soy un bot de análisis de datos que puede ayudarte con:

{EMOJIS['chart']} Análisis de ventas por región, tienda, producto
{EMOJIS['graph']} Gráficos y visualizaciones
{EMOJIS['report']} Informes detallados en Word
{EMOJIS['table']} Tablas y comparativas
{EMOJIS['search']} Búsquedas y filtros complejos

*¿Cómo funciona?*
1. Escribe tu pregunta en lenguaje natural
2. Procesaré tu consulta y te daré resultados
3. Puedes descargar gráficos e informes

*Ejemplos de preguntas:*
• "¿Cuántas ventas hubo en Bogotá?"
• "Top 5 departamentos con más ingresos"
• "Genera un informe de ventas del mes"
• "Compara enero y febrero"

Escribe tu pregunta para comenzar {EMOJIS['search']}
        """
        
        await update.message.reply_text(
            mensaje,
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /ayuda — muestra información de ayuda"""
        mensaje = f"""
*{EMOJIS['info']} Guía de Ayuda*

*Comandos disponibles:*
/start - Iniciar el bot
/ayuda - Mostrar esta ayuda
/sesion - Ver info de tu sesión
/limpiar - Limpiar archivos temporales

*Ejemplos de consultas:*

{EMOJIS['chart']} *Análisis básico:*
  • Ventas por departamento
  • Top 10 referencias más vendidas
  • ¿Cuál es la talla más vendida?

{EMOJIS['graph']} *Con gráficos:*
  • Graficame las ventas por mes
  • Top 5 tiendas - gráfico de barras
  • Evolución de ventas en junio

{EMOJIS['report']} *Informes:*
  • Genera un informe de ventas
  • Informe detallado para Éxito
  • Reporte mensual

*Formato de respuestas:*
- Las respuestas se envían en formato legible
- Los gráficos se envían como imágenes
- Los informes se pueden descargar

¿Necesitas ayuda con algo específico? Escribe tu pregunta {EMOJIS['search']}
        """
        
        await update.message.reply_text(
            mensaje,
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def sesion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /sesion — muestra información de la sesión actual"""
        user_id = update.effective_user.id
        sesion = session_manager.obtener(user_id)
        
        if not sesion:
            await update.message.reply_text(f"{EMOJIS['error']} No hay sesión activa.")
            return
        
        mensaje = f"""
{EMOJIS['info']} *Información de tu sesión*

*Usuario:* {sesion.first_name or sesion.username}
*ID de usuario:* `{sesion.user_id}`
*Chat ID:* `{sesion.chat_id}`
*Turnos procesados:* {sesion.turno}
*Archivos generados:* {len(sesion.archivos_generados)}
*Creada:* {sesion.created_at.strftime('%d/%m/%Y %H:%M')}
        """
        
        if sesion.archivos_generados:
            mensaje += "\n\n*Archivos en esta sesión:*\n"
            for archivo in sesion.archivos_generados[-5:]:  # Últimos 5
                nombre = Path(archivo).name
                mensaje += f"  • {nombre}\n"
        
        await update.message.reply_text(
            mensaje,
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def limpiar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Comando /limpiar — limpia archivos temporales"""
        user_id = update.effective_user.id
        session_manager.limpiar_archivos(user_id)
        
        await update.message.reply_text(
            f"{EMOJIS['success']} Archivos temporales limpiados.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Procesa un mensaje de texto del usuario"""
        user = update.effective_user
        chat_id = update.effective_chat.id
        texto = update.message.text.strip()
        
        # Validar entrada
        if not texto or len(texto) < 3:
            await update.message.reply_text(
                f"{EMOJIS['warning']} Por favor, escribe una pregunta más clara.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Obtener o crear sesión
        sesion = session_manager.obtener_o_crear(
            user_id=user.id,
            chat_id=chat_id,
            username=user.username,
            first_name=user.first_name
        )
        
        # Crear ID de sesión único (basado en user_id)
        session_id = f"telegram_{user.id}"
        
        # Enviar mensaje de "escribiendo..."
        mensaje_espera = await update.message.reply_text(
            f"{EMOJIS['loading']} Procesando tu consulta...\n_(Esto puede tomar unos segundos)_",
            parse_mode=ParseMode.MARKDOWN
        )
        
        try:
            # Enviar consulta al servidor
            inicio = time.time()
            resultado = api_client.enviar_consulta(session_id, texto)
            duracion = time.time() - inicio
            
            # Eliminar mensaje de espera
            await mensaje_espera.delete()
            
            # Procesar resultado
            if not resultado.get('success', True):
                error = resultado.get('error', 'Error desconocido')
                await update.message.reply_text(
                    f"{EMOJIS['error']} *Error:*\n{error}",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            # Formatear respuesta
            respuesta = resultado.get('respuesta', '')
            tipo = resultado.get('tipo', 'consulta')
            imagenes = resultado.get('imagenes', [])
            ruta_excel = resultado.get('ruta_excel', '')
            ruta_docx = resultado.get('ruta_docx', '')
            
            # Dividir mensaje si es muy largo
            mensajes = MessageFormatter.dividir_mensaje_largo(respuesta)
            
            for msg in mensajes:
                await update.message.reply_text(
                    msg,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
            
            # Enviar imágenes si las hay
            if imagenes:
                for imagen in imagenes[:5]:  # Máximo 5 imágenes
                    try:
                        ruta_imagen = Path(imagen)
                        if ruta_imagen.exists():
                            with open(ruta_imagen, 'rb') as f:
                                await update.message.reply_photo(
                                    photo=f,
                                    caption=f"{EMOJIS['chart']} Gráfico generado"
                                )
                    except Exception as e:
                        logger.error(f"Error enviando imagen: {e}")
            
            # Enviar botones de descarga si hay archivos
            if ruta_excel or ruta_docx:
                botones = []
                if ruta_excel:
                    botones.append(
                        InlineKeyboardButton(
                            f"{EMOJIS['download']} Descargar Excel",
                            callback_data=f"download_excel_{user.id}"
                        )
                    )
                if ruta_docx:
                    botones.append(
                        InlineKeyboardButton(
                            f"{EMOJIS['report']} Descargar Informe",
                            callback_data=f"download_docx_{user.id}"
                        )
                    )
                
                if botones:
                    keyboard = InlineKeyboardMarkup([botones])
                    await update.message.reply_text(
                        f"{EMOJIS['file']} Archivos disponibles para descargar:",
                        reply_markup=keyboard
                    )
                    
                    # Guardar rutas en contexto
                    context.user_data['ultima_ruta_excel'] = ruta_excel
                    context.user_data['ultima_ruta_docx'] = ruta_docx
            
            # Incrementar turno
            session_manager.incrementar_turno(user.id)
            
        except Exception as e:
            logger.error(f"Error procesando mensaje: {e}")
            await mensaje_espera.delete()
            await update.message.reply_text(
                f"{EMOJIS['error']} Error inesperado: {str(e)}",
                parse_mode=ParseMode.MARKDOWN
            )
    
    @staticmethod
    async def descargar_archivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Maneja la descarga de archivos"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        await query.answer()
        
        if "excel" in query.data:
            ruta = context.user_data.get('ultima_ruta_excel', '')
            tipo_archivo = "Excel"
        else:
            ruta = context.user_data.get('ultima_ruta_docx', '')
            tipo_archivo = "Informe"
        
        if not ruta:
            await query.edit_message_text(f"{EMOJIS['error']} Archivo no disponible.")
            return
        
        try:
            ruta_local = Path(ruta)
            if not ruta_local.exists():
                await query.edit_message_text(
                    f"{EMOJIS['error']} El archivo no existe."
                )
                return
            
            # Enviar archivo
            with open(ruta_local, 'rb') as f:
                await query.message.reply_document(
                    document=f,
                    filename=ruta_local.name,
                    caption=f"{EMOJIS['download']} {tipo_archivo} generado"
                )
            
            await query.edit_message_text(
                f"{EMOJIS['success']} Archivo enviado."
            )
            
        except Exception as e:
            logger.error(f"Error descargando archivo: {e}")
            await query.edit_message_text(
                f"{EMOJIS['error']} Error al descargar el archivo."
            )
