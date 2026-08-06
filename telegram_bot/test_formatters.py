"""
telegram_bot/test_formatters.py
Pruebas de los formateadores para verificar que los mensajes se ven bien
"""
import sys
import io
from pathlib import Path

# Configurar UTF-8 en stdout
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Agregar ruta base al path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from telegram_bot.formatters import MessageFormatter

def test_tabla_simple():
    """Prueba formato de tabla simple"""
    datos = [
        {'Departamento': 'Bogota', 'Ventas': 1250000, 'Unidades': 850},
        {'Departamento': 'Cali', 'Ventas': 890000, 'Unidades': 620},
        {'Departamento': 'Medellin', 'Ventas': 1120000, 'Unidades': 780},
    ]
    
    tabla = MessageFormatter.formato_tabla_simple(datos)
    print("\n=== TABLA SIMPLE ===")
    print(tabla)
    print()

def test_tabla_markdown():
    """Prueba formato de tabla markdown"""
    datos = [
        {'Mes': 'Enero', 'Ventas': 1250000, 'Crecimiento': '+5%'},
        {'Mes': 'Febrero', 'Ventas': 1420000, 'Crecimiento': '+13.6%'},
        {'Mes': 'Marzo', 'Ventas': 1890000, 'Crecimiento': '+33%'},
    ]
    
    tabla = MessageFormatter.formato_tabla_markdown(datos)
    print("=== TABLA MARKDOWN ===")
    print(tabla)
    print()

def test_dividir_mensaje():
    """Prueba division de mensajes largos"""
    mensaje_largo = "Lorem ipsum " * 500  # Mensaje de ~6000 caracteres
    
    partes = MessageFormatter.dividir_mensaje_largo(mensaje_largo)
    print("=== DIVISION DE MENSAJE ===")
    print(f"Mensaje original: {len(mensaje_largo)} caracteres")
    print(f"Dividido en: {len(partes)} partes")
    for i, parte in enumerate(partes, 1):
        print(f"  Parte {i}: {len(parte)} caracteres")
    print()

def test_formato_titulo():
    """Prueba formato de titulo"""
    titulo = MessageFormatter.formato_titulo("Analisis de Ventas", "[G]")
    print("=== TITULO ===")
    print(titulo)
    print()

def test_formato_seccion():
    """Prueba formato de seccion"""
    seccion = MessageFormatter.formato_seccion(
        "Resultados",
        "Se encontraron 1,250 registros\nCrecimiento: +15% vs mes anterior"
    )
    print("=== SECCION ===")
    print(seccion)
    print()

def test_formato_menu():
    """Prueba formato de menu"""
    menu = MessageFormatter.formato_menu(
        ['Ventas por departamento', 'Top 10 referencias', 'Grafico mensual'],
        'Selecciona un analisis'
    )
    print("=== MENU ===")
    print(menu)
    print()

def test_formato_archivo():
    """Prueba formato de archivo"""
    archivo = MessageFormatter.formato_archivo(
        'Informe_Enero_2024.docx',
        tamaño=2048000,
        tipo='documento'
    )
    print("=== ARCHIVO ===")
    print(archivo)
    print()

def test_formato_estado():
    """Prueba formato de estado de procesamiento"""
    estado = MessageFormatter.formato_estado_procesamiento("Ejecutando consulta", progreso=7)
    print("=== ESTADO ===")
    print(estado)
    print()

def test_resultado_sql():
    """Prueba formato de resultado SQL"""
    resultado = {
        'success': True,
        'total_filas': 5,
        'columns': ['Tienda', 'Ventas', 'Unidades'],
        'rows': [
            ['Exito Centro', '1250000', '850'],
            ['Exito Occidente', '890000', '620'],
            ['Jumbo Bogota', '1120000', '780'],
            ['Olimpica', '650000', '450'],
            ['La Montana', '420000', '300'],
        ]
    }
    
    resultado_fmt = MessageFormatter.formato_resultado_sql(resultado)
    print("=== RESULTADO SQL ===")
    print(resultado_fmt)
    print()

def run_all_tests():
    """Ejecuta todas las pruebas"""
    print("\n" + "="*60)
    print("PRUEBAS DE FORMATEO PARA TELEGRAM")
    print("="*60)
    
    test_formato_titulo()
    test_formato_seccion()
    test_tabla_simple()
    test_tabla_markdown()
    test_formato_menu()
    test_formato_archivo()
    test_formato_estado()
    test_resultado_sql()
    test_dividir_mensaje()
    
    print("\n" + "="*60)
    print("PRUEBAS COMPLETADAS CORRECTAMENTE")
    print("="*60 + "\n")

if __name__ == '__main__':
    run_all_tests()
