"""
session_store.py
Gestiona el estado en memoria de cada sesión de usuario.
Cada sesión contiene:
  - historial: últimos 3 turnos comprimidos (lo que el LLM lee)
  - dataframes: DataFrames pandas activos con su metadata
  - turno_actual: contador incremental
  - ultima_actividad: timestamp para limpieza TTL
"""
import time
import threading
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
MAX_TURNOS_HISTORIAL   = 3    # Turnos que el LLM puede leer
MAX_DFS_ACTIVOS        = 5    # Límite duro de DataFrames simultáneos
VENTANA_OBSOLESCENCIA  = 3    # Turnos sin uso → df candidato a limpieza
MIN_DFS_PARA_LIMPIAR   = 3    # Solo limpiar si hay más de este número
TTL_SESION_SEGUNDOS    = 600  # 10 min sin actividad → sesión huérfana. Ver
                               # TTL_SESION_SEGUNDOS en
                               # telegram_bot/session_manager.py, mismo valor.
MAX_CHARS_PREGUNTA_LLM = 300  # Tope de la pregunta al serializarla para el LLM
                               # (no se trunca el valor almacenado en Turno,
                               # solo la copia que ve el modelo — evita que un
                               # mensaje inusualmente largo se repita entero
                               # en cada prompt durante las próximas 3 turnos)
MAX_BUSQUEDAS_WEB_SESION = 3  # Tope de llamadas a Tavily por sesión — cada
                               # búsqueda es una petición HTTP paga; sin este
                               # límite, una sesión larga o un mensaje que el
                               # enrutador reclasifica en cada turno como
                               # "necesita_busqueda_web" podría disparar una
                               # búsqueda por turno indefinidamente.


# ---------------------------------------------------------------------------
# Estructuras de datos
# ---------------------------------------------------------------------------

@dataclass
class MetaDF:
    """
    Todo lo que el LLM necesita saber sobre un DataFrame.
    El DataFrame real NUNCA se envía al LLM.
    """
    nombre: str
    descripcion: str
    columnas: dict          # {nombre_col: tipo_str}
    total_filas: int
    metricas: dict          # {total_X, top_1, min, max, ...}
    sql_original: str       # Para re-ejecutar si el df es prescindido
    turno_creado: int
    turno_ultimo_uso: int
    estado: str = 'activo'  # 'activo' | 'obsoleto'


@dataclass
class Turno:
    """
    Resumen comprimido de un turno — lo que el LLM lee en el historial.
    """
    turno: int
    pregunta: str
    ruta: str               # NUEVA_CONSULTA | REFINAMIENTO | SOBRE_DATOS | CONVERSACIONAL
    df_creado: Optional[str]
    df_usado: Optional[str]
    resumen_respuesta: str  # 1-2 líneas máximo
    periodo_detectado: str
    filtros_activos: list


@dataclass
class Sesion:
    session_id: str
    historial: list = field(default_factory=list)   # Lista de Turno
    dataframes: dict = field(default_factory=dict)  # {nombre: (MetaDF, pd.DataFrame)}
    turno_actual: int = 0
    creada: float = field(default_factory=time.time)
    ultima_actividad: float = field(default_factory=time.time)
    busquedas_web: int = 0  # contador de llamadas a Tavily en esta sesión


# ---------------------------------------------------------------------------
# Digest completo — para el modo conversacional (Fase 4.1)
# ---------------------------------------------------------------------------

def _digest_completo(df: pd.DataFrame) -> dict:
    """
    Digest sobre TODAS las filas del DataFrame (no una muestra) para que el
    modo conversacional pueda citar cifras concretas sin recibir las filas
    crudas. Cuesta ~300 tokens sea el df de 50 o de 1000 filas.

    Reutiliza pre_computar_metricas (totales, min/max, concentración top-10,
    variaciones %, tendencia, outliers) y añade lo que ese cálculo no cubre:
    cola (bottom-5), nulos/ceros, cardinalidad categórica y cuartiles.
    """
    if df is None or df.empty:
        return {}

    from src.orquestador import pre_computar_metricas

    columnas = df.columns.tolist()
    # .astype(object) es necesario: en una columna float, .where(..., None)
    # sola no reemplaza NaN por None porque pandas preserva el dtype float
    # (que no puede representar None) y el NaN sobrevive silenciosamente.
    filas = df.astype(object).where(pd.notna(df), None).values.tolist()
    digest = pre_computar_metricas({'columns': columnas, 'rows': filas})

    cols_num = df.select_dtypes(include='number').columns.tolist()
    cols_cat = df.select_dtypes(include='object').columns.tolist()

    # Cola: las filas con menor valor — pre_computar_metricas solo da el top-10.
    # Etiqueta compuesta con TODAS las columnas categóricas (no solo la
    # primera), para no perder de vista, ej., cuál categoría dentro de una
    # zona es la que casi no vende.
    if cols_cat and cols_num:
        bottom = {}
        for col in cols_num[:3]:
            peores = df.nsmallest(5, col)[cols_cat + [col]]
            bottom[col] = [
                {
                    'categoria': ' / '.join(str(row[c]).strip() for c in cols_cat if str(row[c]).strip()),
                    'valor':     round(float(row[col]), 2),
                }
                for _, row in peores.iterrows()
            ]
        if bottom:
            digest['bottom_5'] = bottom

    # Nulos y ceros por columna
    nulos_ceros = {}
    for col in columnas:
        nulos = int(df[col].isna().sum())
        ceros = int((df[col] == 0).sum()) if col in cols_num else 0
        if nulos or ceros:
            nulos_ceros[col] = {'nulos': nulos, 'ceros': ceros}
    if nulos_ceros:
        digest['nulos_y_ceros'] = nulos_ceros

    # Cardinalidad de columnas categóricas (con los valores si son pocos)
    cardinalidad = {}
    for col in cols_cat:
        n_unicos = int(df[col].nunique())
        entrada = {'valores_unicos': n_unicos}
        if n_unicos <= 15:
            entrada['valores'] = sorted(str(v) for v in df[col].dropna().unique())
        cardinalidad[col] = entrada
    if cardinalidad:
        digest['cardinalidad'] = cardinalidad

    # Cuartiles de columnas numéricas
    cuartiles = {}
    for col in cols_num[:3]:
        q = df[col].quantile([0.25, 0.5, 0.75])
        cuartiles[col] = {
            'q1':      round(float(q[0.25]), 2),
            'mediana': round(float(q[0.5]), 2),
            'q3':      round(float(q[0.75]), 2),
        }
    if cuartiles:
        digest['cuartiles'] = cuartiles

    return digest


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------

class SessionStore:
    """
    Almacén en memoria de todas las sesiones activas.
    Thread-safe con un lock por operación de escritura.
    """

    def __init__(self):
        self._sesiones: dict[str, Sesion] = {}
        self._lock = threading.Lock()
        # Arrancar hilo de limpieza TTL cada 30 minutos
        self._limpiar_hilo = threading.Thread(
            target=self._ciclo_limpieza_ttl,
            daemon=True,
        )
        self._limpiar_hilo.start()

    # ------------------------------------------------------------------
    # Sesiones
    # ------------------------------------------------------------------

    def obtener_o_crear(self, session_id: str) -> Sesion:
        with self._lock:
            if session_id not in self._sesiones:
                self._sesiones[session_id] = Sesion(session_id=session_id)
            sesion = self._sesiones[session_id]
            sesion.ultima_actividad = time.time()
            return sesion

    def existe(self, session_id: str) -> bool:
        return session_id in self._sesiones

    def eliminar_sesion(self, session_id: str):
        with self._lock:
            self._sesiones.pop(session_id, None)

    def total_sesiones_activas(self) -> int:
        return len(self._sesiones)

    # ------------------------------------------------------------------
    # Historial de turnos
    # ------------------------------------------------------------------

    def agregar_turno(self, session_id: str, turno: Turno):
        """Agrega turno al historial. Mantiene solo los últimos MAX_TURNOS_HISTORIAL."""
        sesion = self.obtener_o_crear(session_id)
        with self._lock:
            sesion.historial.append(turno)
            # Sliding window: mantener solo los últimos N turnos
            if len(sesion.historial) > MAX_TURNOS_HISTORIAL:
                sesion.historial = sesion.historial[-MAX_TURNOS_HISTORIAL:]
            sesion.turno_actual += 1

    def obtener_historial(self, session_id: str) -> list[Turno]:
        sesion = self.obtener_o_crear(session_id)
        return list(sesion.historial)

    # ------------------------------------------------------------------
    # Límite de búsquedas web (Tavily) por sesión
    # ------------------------------------------------------------------

    def puede_buscar_web(self, session_id: str) -> bool:
        """True si la sesión aún no alcanzó MAX_BUSQUEDAS_WEB_SESION."""
        sesion = self.obtener_o_crear(session_id)
        with self._lock:
            return sesion.busquedas_web < MAX_BUSQUEDAS_WEB_SESION

    def registrar_busqueda_web(self, session_id: str):
        """Incrementa el contador tras una llamada real a Tavily (no al omitirla)."""
        sesion = self.obtener_o_crear(session_id)
        with self._lock:
            sesion.busquedas_web += 1

    def hay_historial(self, session_id: str) -> bool:
        """True si la sesión tiene al menos un turno previo."""
        if not self.existe(session_id):
            return False
        return len(self._sesiones[session_id].historial) > 0

    # ------------------------------------------------------------------
    # DataFrames
    # ------------------------------------------------------------------

    def crear_df(
        self,
        session_id: str,
        nombre: str,
        df: pd.DataFrame,
        descripcion: str,
        sql_original: str,
    ) -> MetaDF:
        """
        Registra un nuevo DataFrame en la sesión.
        Calcula métricas comprimidas automáticamente.
        Aplica limpieza de obsoletos después de insertar.
        """
        sesion = self.obtener_o_crear(session_id)
        metricas = self._calcular_metricas(df)
        columnas = {col: str(df[col].dtype) for col in df.columns}

        meta = MetaDF(
            nombre=nombre,
            descripcion=descripcion,
            columnas=columnas,
            total_filas=len(df),
            metricas=metricas,
            sql_original=sql_original,
            turno_creado=sesion.turno_actual,
            turno_ultimo_uso=sesion.turno_actual,
        )

        with self._lock:
            sesion.dataframes[nombre] = (meta, df)

        # Limpiar obsoletos después de agregar
        self._limpiar_dfs_obsoletos(session_id)
        return meta

    def obtener_df(self, session_id: str, nombre: str) -> Optional[tuple]:
        """
        Retorna (MetaDF, pd.DataFrame) o None si no existe o está obsoleto.
        Actualiza turno_ultimo_uso.
        """
        sesion = self.obtener_o_crear(session_id)
        entrada = sesion.dataframes.get(nombre)
        if not entrada:
            return None
        meta, df = entrada
        if meta.estado == 'obsoleto':
            return None
        # Actualizar último uso
        with self._lock:
            meta.turno_ultimo_uso = sesion.turno_actual
        return meta, df

    def listar_dfs_activos(self, session_id: str) -> list[MetaDF]:
        """Retorna la metadata de todos los dfs activos (sin los DataFrames reales)."""
        if not self.existe(session_id):
            return []
        sesion = self._sesiones[session_id]
        return [
            meta for meta, _ in sesion.dataframes.values()
            if meta.estado == 'activo'
        ]

    def obtener_meta_df(self, session_id: str, nombre: str) -> Optional[MetaDF]:
        sesion = self.obtener_o_crear(session_id)
        entrada = sesion.dataframes.get(nombre)
        if not entrada:
            return None
        return entrada[0]

    def siguiente_nombre_df(self, session_id: str) -> str:
        """Genera el próximo nombre disponible: df_1, df_2, ..."""
        sesion = self.obtener_o_crear(session_id)
        n = len(sesion.dataframes) + 1
        # Asegurar que no haya colisión
        nombre = f'df_{n}'
        while nombre in sesion.dataframes:
            n += 1
            nombre = f'df_{n}'
        return nombre

    # ------------------------------------------------------------------
    # Limpieza de DataFrames
    # ------------------------------------------------------------------

    def _limpiar_dfs_obsoletos(self, session_id: str):
        """
        Marca como obsoleto y libera memoria de DataFrames que:
          1. turno_actual - turno_ultimo_uso > VENTANA_OBSOLESCENCIA
          2. Hay más de MIN_DFS_PARA_LIMPIAR dfs activos
        También aplica límite duro MAX_DFS_ACTIVOS.
        """
        sesion = self._sesiones.get(session_id)
        if not sesion:
            return

        with self._lock:
            activos = [
                (nombre, meta, df)
                for nombre, (meta, df) in sesion.dataframes.items()
                if meta.estado == 'activo'
            ]
            n_activos = len(activos)

            # Limpieza por ventana de obsolescencia
            if n_activos > MIN_DFS_PARA_LIMPIAR:
                for nombre, meta, df in activos:
                    edad = sesion.turno_actual - meta.turno_ultimo_uso
                    if edad > VENTANA_OBSOLESCENCIA:
                        meta.estado = 'obsoleto'
                        # Reemplazar df con None para liberar memoria
                        sesion.dataframes[nombre] = (meta, None)
                        n_activos -= 1
                        if n_activos <= MIN_DFS_PARA_LIMPIAR:
                            break

            # Límite duro: si sigue habiendo más de MAX_DFS_ACTIVOS
            activos_restantes = [
                (nombre, meta)
                for nombre, (meta, _) in sesion.dataframes.items()
                if meta.estado == 'activo'
            ]
            if len(activos_restantes) > MAX_DFS_ACTIVOS:
                # Eliminar el más antiguo
                mas_antiguo = min(activos_restantes, key=lambda x: x[1].turno_creado)
                nombre_eliminar = mas_antiguo[0]
                meta_elim = sesion.dataframes[nombre_eliminar][0]
                meta_elim.estado = 'obsoleto'
                sesion.dataframes[nombre_eliminar] = (meta_elim, None)

    # ------------------------------------------------------------------
    # Contexto comprimido para el enrutador
    # ------------------------------------------------------------------

    def contexto_para_llm(self, session_id: str) -> dict:
        """
        Retorna el contexto comprimido que el enrutador/LLM puede leer.
        Solo metadata — nunca DataFrames reales ni filas completas.
        """
        if not self.existe(session_id):
            return {'historial': [], 'dataframes_activos': []}

        sesion = self._sesiones[session_id]
        historial_dict = [
            {
                'turno':             t.turno,
                'pregunta':          (
                    t.pregunta if len(t.pregunta) <= MAX_CHARS_PREGUNTA_LLM
                    else t.pregunta[:MAX_CHARS_PREGUNTA_LLM] + '…'
                ),
                'ruta':              t.ruta,
                'df_creado':         t.df_creado,
                'df_usado':          t.df_usado,
                'resumen':           t.resumen_respuesta,
                'periodo':           t.periodo_detectado,
                'filtros':           t.filtros_activos,
            }
            for t in sesion.historial
        ]

        dfs_dict = [
            {
                'nombre':      meta.nombre,
                'descripcion': meta.descripcion,
                'columnas':    meta.columnas,
                'total_filas': meta.total_filas,
                'metricas':    meta.metricas,
                'turno_creado': meta.turno_creado,
            }
            for meta, _ in sesion.dataframes.values()
            if meta.estado == 'activo'
        ]

        return {
            'historial':          historial_dict,
            'dataframes_activos': dfs_dict,
            'turno_actual':       sesion.turno_actual,
        }

    # ------------------------------------------------------------------
    # Digest completo (para el modo conversacional)
    # ------------------------------------------------------------------

    def calcular_digest(self, session_id: str, nombre: str) -> dict:
        """
        Digest estadístico sobre TODAS las filas del df (no una muestra) —
        a diferencia de contexto_para_llm(), que solo da metadata. Se calcula
        bajo demanda (no en cada turno) porque es más caro que _calcular_metricas.
        """
        entrada = self.obtener_df(session_id, nombre)
        if not entrada:
            return {}
        _, df = entrada
        return _digest_completo(df)

    # ------------------------------------------------------------------
    # Métricas comprimidas
    # ------------------------------------------------------------------

    @staticmethod
    def _calcular_metricas(df: pd.DataFrame) -> dict:
        """
        Calcula métricas comprimidas del DataFrame.
        El resultado va al contexto del LLM (~4-6 valores por df).
        """
        metricas = {}
        if df.empty:
            return metricas

        # Columnas numéricas
        cols_num = df.select_dtypes(include='number').columns.tolist()
        # Primera columna de texto como etiqueta
        cols_str = df.select_dtypes(include='object').columns.tolist()
        col_label = cols_str[0] if cols_str else None

        for col in cols_num[:3]:  # Máximo 3 columnas numéricas en métricas
            total = float(df[col].sum())
            metricas[f'total_{col}'] = round(total, 2)

            if col_label:
                idx_max = df[col].idxmax()
                idx_min = df[col].idxmin()
                metricas[f'top_1_{col}'] = str(df.loc[idx_max, col_label])
                metricas[f'min_1_{col}'] = str(df.loc[idx_min, col_label])

        return metricas

    # ------------------------------------------------------------------
    # Limpieza TTL (hilo daemon)
    # ------------------------------------------------------------------

    def _ciclo_limpieza_ttl(self):
        """Limpia sesiones huérfanas. El ciclo corre más seguido que el TTL
        para que una sesión no quede viva de más esperando el próximo barrido."""
        while True:
            time.sleep(120)  # 2 minutos
            ahora = time.time()
            with self._lock:
                expiradas = [
                    sid for sid, ses in self._sesiones.items()
                    if ahora - ses.ultima_actividad > TTL_SESION_SEGUNDOS
                ]
                for sid in expiradas:
                    del self._sesiones[sid]
            if expiradas:
                print(f'[SessionStore] Limpieza TTL: {len(expiradas)} sesión(es) eliminada(s).')


# Instancia global — importada por server.py
store = SessionStore()
