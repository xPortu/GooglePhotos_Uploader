
import os
   
import time
import shutil
import threading
# import mimetypes

def print_banner():
    print(r"""
         ____
__  __  / __ \____  _____/ /___  __
\ \/ / / /_/ / __ \/ ___/ __/ / / /
 >  < / ____/ /_/ / /  / /_/ /_/ /
/_/\_\/_/    \____/_/   \__/\__,_/

┌──────────────────────────────────┐
│              xPortu              │
│      GooglePhotos_Uploader       │
└──────────────────────────────────┘
""")


def print_startup_info():
    print_banner()

    print(f"UID             : {os.getuid()}")
    print(f"GID             : {os.getgid()}")
    print(f"WATCHED_FOLDER  : {WATCHED_FOLDER}")
    print(f"LOG_PATH        : {LOG_PATH}")
    print(f"FAILED_FOLDER   : {FAILED_FOLDER}")
    print()

    if os.access(WATCHED_FOLDER, os.W_OK):
        print(f"[OK] Escritura en {WATCHED_FOLDER}")
    else:
        print(f"[WARN] Sin escritura en {WATCHED_FOLDER}")

    if os.access(LOG_PATH, os.W_OK):
        print(f"[OK] Escritura en {LOG_PATH}")
    else:
        print(f"[WARN] Sin escritura en {LOG_PATH}")

    print()


from queue import Queue, Empty
from datetime import datetime

from watchdog.observers.polling import PollingObserver as Observer
from watchdog.events import FileSystemEventHandler

from gpmc import Client

# Registrar Motion Photo .MP como vídeo MP4 sin renombrarlo
# mimetypes.add_type("video/mp4", ".mp")
# mimetypes.add_type("video/mp4", ".MP")

WATCHED_FOLDER = os.environ.get("WATCHED_FOLDER", "/data")
AUTH_DATA = os.environ.get("AUTH_DATA", "")

# NUEVO: ruta para logs (directorio). Ej: /logs
LOG_PATH = os.environ.get("LOG_PATH", "/logs")
os.makedirs(LOG_PATH, exist_ok=True)

FAILED_FOLDER = os.path.join(WATCHED_FOLDER, "_failed")
os.makedirs(FAILED_FOLDER, exist_ok=True)

VALID_EXT = (".jpg", ".jpeg", ".png", ".heic", ".mp4")
# VALID_EXT = (".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mp")

client = Client(auth_data=AUTH_DATA)

# Cola de trabajo para no bloquear el observer
work_q = Queue()
in_flight = set()
in_flight_lock = threading.Lock()

# Lock para escritura concurrente de logs
log_lock = threading.Lock()


def _today_str() -> str:
    # Fecha local del contenedor (si quieres TZ, se ajusta en Docker con TZ=Europe/Madrid, etc.)
    return datetime.now().strftime("%Y-%m-%d")


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_file(success: bool) -> str:
    date = _today_str()
    if success:
        return os.path.join(LOG_PATH, f"uploads_{date}.log")
    return os.path.join(LOG_PATH, f"errors_{date}.log")


def log_success(path: str, output):
    try:
        line = f"{_ts()} | OK | {path} | {output}\n"

        with log_lock:
            with open(_log_file(True), "a", encoding="utf-8") as f:
                f.write(line)

    except Exception as e:
        print(f"[WARN] Error escribiendo uploads log: {e}")


def log_error(path: str, err: str, extra: str = ""):
    try:
        extra_part = f" | {extra}" if extra else ""
        line = f"{_ts()} | ERR | {path} | {err}{extra_part}\n"

        with log_lock:
            with open(_log_file(False), "a", encoding="utf-8") as f:
                f.write(line)

    except Exception as e:
        print(f"[WARN] Error escribiendo errors log: {e}")


def is_media_file(path: str) -> bool:
    return os.path.isfile(path) and path.lower().endswith(VALID_EXT)


def wait_until_stable(path: str, checks: int = 3, interval: float = 1.0, min_age: float = 0.0) -> bool:
    """
    Devuelve True si el archivo parece 'estable':
    - existe
    - su tamaño no cambia durante 'checks' mediciones separadas por 'interval'
    - opcional: min_age para evitar pillar archivos recién creados
    """
    try:
        if min_age > 0:
            age = time.time() - os.path.getmtime(path)
            if age < min_age:
                time.sleep(min_age - age)

        last_size = -1
        stable_count = 0

        for _ in range(checks * 2):  # margen extra
            if not os.path.exists(path):
                return False

            size = os.path.getsize(path)
            if size == last_size and size > 0:
                stable_count += 1
                if stable_count >= checks:
                    return True
            else:
                stable_count = 0
                last_size = size

            time.sleep(interval)

        return False
    except Exception:
        return False


def safe_move_to_failed(path: str, reason: str):
    try:
        base = os.path.basename(path)
        target = os.path.join(FAILED_FOLDER, base)
        # Evitar colisiones
        if os.path.exists(target):
            name, ext = os.path.splitext(base)
            target = os.path.join(FAILED_FOLDER, f"{name}_{int(time.time())}{ext}")

        shutil.move(path, target)
        print(f"[FAILED] Movido a {target} | Motivo: {reason}")
        # NUEVO: log de fallo (incluyendo destino)
        log_error(path, reason, extra=f"moved_to={target}")
    except Exception as e:
        print(f"[FAILED] No se pudo mover {path} a failed: {e}")
        # NUEVO: log de fallo de movimiento a failed
        log_error(path, reason, extra=f"move_failed={e}")


def process_file(path: str):
    # Espera a que se termine de copiar
    if not wait_until_stable(path, checks=3, interval=1.0, min_age=0.0):
        # Reintenta más tarde (puede estar copiándose)
        print(f"[WAIT] Archivo no estable aún, requeue: {path}")
        time.sleep(1)
        enqueue(path)
        return
   
    print(f"[UPLOAD] Subiendo: {path}")
    try:
        output = client.upload(target=path, show_progress=True)
        # gpmc devuelve {} cuando internamente falla una subida
        if not output:
            raise Exception("Google Photos devolvió resultado vacío")
        
        print(f"[OK] Subido: {output}")

        # NUEVO: log de éxito
        log_success(path, output)

        # Borrado con reintentos
        max_retries = 5
        for attempt in range(max_retries):
            try:
                os.remove(path)
                print(f"[CLEAN] Archivo eliminado: {path}")
                break
            except PermissionError:
                time.sleep(0.5 * (attempt + 1))
        else:
            print(f"[WARN] No se pudo borrar tras reintentos: {path}")
            # Esto no es un fallo de subida, pero lo registramos como aviso en errores
            log_error(path, "upload_ok_but_delete_failed", extra="warn=delete_failed")

    except Exception as e:
        # IMPORTANTE: no borrar en error
        print(f"[ERR] Falló subida: {path} | {e}")
        # NUEVO: log de error
        log_error(path, str(e))
        if os.path.exists(path):
            safe_move_to_failed(path, str(e))


def worker():
    while True:
        try:
            path = work_q.get(timeout=1)

        except Empty:
            continue

        try:
            process_file(path)

        except Exception as e:
            print(f"[FATAL] Error inesperado procesando {path}: {e}")

        finally:
            with in_flight_lock:
                in_flight.discard(path)

            work_q.task_done()


def enqueue(path: str):

    if path.startswith(FAILED_FOLDER):
        return

    if not is_media_file(path):
        return

    with in_flight_lock:
        if path in in_flight:
            return

        in_flight.add(path)

    work_q.put(path)


class PhotoHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        enqueue(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        # En rename/copia-atómica, el destino es el final bueno
        enqueue(event.dest_path)

    def on_modified(self, event):
        # Útil si hay sobrescritura o escritura incremental
        if event.is_directory:
            return
        enqueue(event.src_path)


def initial_scan():
    print("[SCAN] Escaneo inicial...")
    for root, dirs, files in os.walk(WATCHED_FOLDER):
        
        if root.startswith(FAILED_FOLDER):
            continue

        for f in files:
            p = os.path.join(root, f)
            if is_media_file(p):
                enqueue(p)

def periodic_rescan(interval_sec: int = 60):
    while True:
        time.sleep(interval_sec)
        # “Red de seguridad” para polling + sync cross-container
        initial_scan()


if __name__ == "__main__":
    print_startup_info()
    
    # Arranca worker
    t = threading.Thread(target=worker, daemon=True)
    t.start()

    # Escaneo inicial (por si había cosas antes de arrancar)
    initial_scan()

    # Rescan periódico (opcional)
    threading.Thread(target=periodic_rescan, args=(60,), daemon=True).start()

    event_handler = PhotoHandler()
    observer = Observer(timeout=0.5)  # polling más frecuente
    observer.schedule(event_handler, WATCHED_FOLDER, recursive=True)
    observer.start()

    print(f"Monitorizando {WATCHED_FOLDER}...")
    print(f"Logs en {LOG_PATH} (uploads_YYYY-MM-DD.log / errors_YYYY-MM-DD.log)")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()
