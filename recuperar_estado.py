import sys
import os
import json
import argparse
from datetime import datetime, timezone
from config import Config

def atomic_write(filepath: str, data: str):
    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)

def get_control_file_paths(state_file: str) -> tuple[str, str, str]:
    state_file = os.path.abspath(state_file or Config.LOCAL_STATE_FILE)
    base, _ = os.path.splitext(state_file)
    initialized_file = f"{base}.initialized"
    recovery_required_file = f"{base}.recovery_required"
    return state_file, initialized_file, recovery_required_file

def restore_backup(backup_path: str, state_file_override: str = None):
    state_file, initialized_file, recovery_required_file = get_control_file_paths(state_file_override)

    if not os.path.exists(backup_path):
        print(f"[ERROR] El archivo de backup '{backup_path}' no existe.")
        sys.exit(1)

    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            content = f.read()
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("El contenido del backup debe ser un objeto JSON (dict).")
    except Exception as e:
        print(f"[ERROR] El backup '{backup_path}' no contiene un JSON valido: {e}")
        sys.exit(1)

    # Atomic write to state file
    formatted_json = json.dumps(parsed, indent=4, ensure_ascii=False)
    atomic_write(state_file, formatted_json)
    print(f"[OK] Estado restaurado con exito desde '{backup_path}' en '{state_file}'.")

    # Remove recovery_required file only after success
    if os.path.exists(recovery_required_file):
        try:
            os.remove(recovery_required_file)
            print(f"[OK] Archivo de control '{recovery_required_file}' eliminado.")
        except Exception as e:
            print(f"[ERROR] No se pudo eliminar el archivo de control '{recovery_required_file}': {e}")
            sys.exit(1)

    # Maintain initialized file
    if not os.path.exists(initialized_file):
        atomic_write(initialized_file, datetime.now(timezone.utc).isoformat())

    print("[OK] Recuperacion manual completada. Puede reiniciar el bot.")

def reset_state(confirm_token: str, state_file_override: str = None):
    if confirm_token != "RESET_STATE":
        print("[ERROR] Para reiniciar el estado debes incluir exactamente '--confirm RESET_STATE'.")
        sys.exit(1)

    state_file, initialized_file, recovery_required_file = get_control_file_paths(state_file_override)

    atomic_write(state_file, json.dumps({}, indent=4, ensure_ascii=False))
    print(f"[WARN] ADVERTENCIA: Se ha creado un estado vacio en '{state_file}'. Los archivos antiguos podrian volver a procesarse.")

    if os.path.exists(recovery_required_file):
        try:
            os.remove(recovery_required_file)
            print(f"[OK] Archivo de control '{recovery_required_file}' eliminado.")
        except Exception as e:
            print(f"[ERROR] No se pudo eliminar el archivo de control '{recovery_required_file}': {e}")
            sys.exit(1)

    if not os.path.exists(initialized_file):
        atomic_write(initialized_file, datetime.now(timezone.utc).isoformat())

    print("[OK] Reinicio de estado completado con exito.")

def main():
    parser = argparse.ArgumentParser(description="Script de recuperacion manual para StateStore.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--restore", help="Ruta al archivo de backup .json a restaurar.")
    group.add_argument("--reset", action="store_true", help="Crear un nuevo estado vacio.")
    
    parser.add_argument("--confirm", help="Token de confirmacion obligatoria para --reset (debe ser RESET_STATE).")
    parser.add_argument("--state-file", help="Ruta personalizada al archivo de estado (por defecto Config.LOCAL_STATE_FILE).")

    args = parser.parse_args()

    if args.restore:
        restore_backup(args.restore, state_file_override=args.state_file)
    elif args.reset:
        reset_state(args.confirm or "", state_file_override=args.state_file)

if __name__ == "__main__":
    main()
