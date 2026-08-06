"""
telegram_bot/formatters.py
Formateadores para convertir respuestas a formato legible en Telegram
"""
import re
from typing import List, Dict, Any, Optional
from telegram_bot.config import EMOJIS, MAX_MESSAGE_LENGTH

class MessageFormatter:
    """Formatea mensajes para Telegram"""
    
    @staticmethod
    def formato_titulo(titulo: str, emoji: str = '📊') -> str:
        """Formatea un título con emoji"""
        return f"{emoji} *{titulo}*"
    
    @staticmethod
    def formato_seccion(titulo: str, contenido: str) -> str:
        """Formatea una sección con título y contenido"""
        linea_separadora = "─" * 30
        return f"\n{linea_separadora}\n*{titulo}*\n{linea_separadora}\n{contenido}"
    
    @staticmethod
    def formato_tabla_simple(datos: List[Dict[str, Any]], max_ancho: int = 80) -> str:
        """Convierte un DataFrame/lista de datos a tabla de texto plano para Telegram"""
        if not datos:
            return "Sin datos"
        
        # Si es un DataFrame, convertir a lista de dicts
        if hasattr(datos, 'to_dict'):
            datos = datos.to_dict('records')
        
        # Obtener columnas
        columnas = list(datos[0].keys()) if datos else []
        
        # Calcular ancho de columnas
        anchos = {}
        for col in columnas:
            # Ancho máximo: nombre de columna o contenido más largo
            ancho_col = len(str(col))
            for fila in datos:
                ancho_col = max(ancho_col, len(str(fila.get(col, ''))))
            anchos[col] = min(ancho_col, 20)  # Máximo 20 caracteres por columna
        
        # Construir tabla
        linea_separadora = "─" * (sum(anchos.values()) + len(columnas) * 3 + 1)
        
        # Encabezado
        encabezado = "│ " + " │ ".join(
            f"{col:<{anchos[col]}}" for col in columnas
        ) + " │"
        
        tabla = f"{linea_separadora}\n{encabezado}\n{linea_separadora}\n"
        
        # Filas
        for fila in datos[:20]:  # Máximo 20 filas para no saturar
            fila_texto = "│ " + " │ ".join(
                f"{str(fila.get(col, '')):<{anchos[col]}}" for col in columnas
            ) + " │"
            tabla += fila_texto + "\n"
        
        if len(datos) > 20:
            tabla += f"\n... y {len(datos) - 20} registros más\n"
        
        tabla += linea_separadora
        
        return tabla
    
    @staticmethod
    def formato_tabla_markdown(datos: List[Dict[str, Any]]) -> str:
        """Convierte datos a tabla en markdown para Telegram"""
        if not datos:
            return "Sin datos"
        
        # Si es un DataFrame, convertir a lista de dicts
        if hasattr(datos, 'to_dict'):
            datos = datos.to_dict('records')
        
        columnas = list(datos[0].keys()) if datos else []
        
        # Encabezado markdown
        tabla = "| " + " | ".join(columnas) + " |\n"
        tabla += "| " + " | ".join("---" for _ in columnas) + " |\n"
        
        # Filas (máximo 15 para no saturar)
        for fila in datos[:15]:
            valores = [str(fila.get(col, '')).replace('|', '\\|') for col in columnas]
            tabla += "| " + " | ".join(valores) + " |\n"
        
        if len(datos) > 15:
            tabla += f"\n_(Mostrando 15 de {len(datos)} registros)_"
        
        return tabla
    
    @staticmethod
    def formato_resultado_sql(resultado: Dict[str, Any]) -> str:
        """Formatea un resultado de SQL para Telegram"""
        if not resultado.get('success'):
            return f"{EMOJIS['error']} *Error en la consulta:*\n`{resultado.get('error', 'Error desconocido')}`"
        
        respuesta = f"{EMOJIS['success']} *Consulta ejecutada exitosamente*\n"
        
        # Información básica
        total = resultado.get('total_filas', 0)
        respuesta += f"_Total de registros: {total}_\n"
        
        # Datos en tabla
        if resultado.get('rows'):
            respuesta += "\n"
            # Usar formato simple de tabla
            datos = []
            columnas = resultado.get('columns', [])
            for fila in resultado.get('rows', [])[:20]:
                datos.append(dict(zip(columnas, fila)))
            
            respuesta += MessageFormatter.formato_tabla_simple(datos)
        
        return respuesta
    
    @staticmethod
    def formato_respuesta_general(
        respuesta: str,
        tipo: str = 'consulta',
        imagenes: Optional[List[str]] = None,
        duracion: float = 0.0
    ) -> Dict[str, Any]:
        """Formatea una respuesta general del pipeline"""
        
        respuesta_formateada = respuesta.strip()
        
        # Agregar información de tipo
        if tipo == 'informe':
            prefijo = f"{EMOJIS['report']} *Informe generado*"
        elif tipo == 'consulta':
            prefijo = f"{EMOJIS['chart']} *Resultado de la consulta*"
        elif tipo == 'error':
            prefijo = f"{EMOJIS['error']} *Error al procesar la consulta*"
        else:
            prefijo = f"{EMOJIS['info']} *Respuesta*"
        
        # Agregar duracion si es significativa
        if duracion > 0.5:
            sufijo = f"\n\n_⏱️ Procesado en {duracion:.1f} segundos_"
        else:
            sufijo = ""
        
        # Combinar
        mensaje_completo = f"{prefijo}\n\n{respuesta_formateada}{sufijo}"
        
        return {
            'mensaje': mensaje_completo,
            'tipo': tipo,
            'imagenes': imagenes or [],
            'largo': len(mensaje_completo)
        }
    
    @staticmethod
    def dividir_mensaje_largo(mensaje: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
        """Divide un mensaje largo en varias partes"""
        if len(mensaje) <= max_length:
            return [mensaje]
        
        mensajes = []
        partes = mensaje.split('\n')
        mensaje_actual = ''
        
        for parte in partes:
            if len(mensaje_actual) + len(parte) + 1 <= max_length:
                mensaje_actual += parte + '\n'
            else:
                if mensaje_actual:
                    mensajes.append(mensaje_actual.strip())
                mensaje_actual = parte + '\n'
        
        if mensaje_actual:
            mensajes.append(mensaje_actual.strip())
        
        return mensajes
    
    @staticmethod
    def formato_menu(opciones: List[str], titulo: str = "Selecciona una opción") -> str:
        """Formatea un menú de opciones"""
        menu = f"*{titulo}*\n\n"
        for i, opcion in enumerate(opciones, 1):
            menu += f"`{i}` {opcion}\n"
        return menu
    
    @staticmethod
    def formato_estado_procesamiento(etapa: str, progreso: int = 0) -> str:
        """Formatea el estado de un procesamiento"""
        barra_progreso = "▓" * progreso + "░" * (10 - progreso)
        return f"{EMOJIS['loading']} *Procesando...*\n[{barra_progreso}] {etapa}"
    
    @staticmethod
    def limpiar_html(texto: str) -> str:
        """Limpia etiquetas HTML del texto"""
        # Remover etiquetas HTML comunes
        texto = re.sub(r'<[^>]+>', '', texto)
        # Remover entidades HTML
        texto = re.sub(r'&\w+;', '', texto)
        return texto
    
    @staticmethod
    def formato_archivo(nombre_archivo: str, tamaño: int = 0, tipo: str = 'documento') -> str:
        """Formatea información de un archivo"""
        emoji_tipo = {
            'documento': EMOJIS['file'],
            'imagen': '🖼️',
            'excel': '📊',
            'pdf': '📄',
            'grafico': EMOJIS['chart']
        }.get(tipo, EMOJIS['file'])
        
        tamaño_str = f"{tamaño / (1024*1024):.1f} MB" if tamaño > 1024*1024 else f"{tamaño / 1024:.1f} KB"
        return f"{emoji_tipo} {nombre_archivo}\n_Tamaño: {tamaño_str}_"
