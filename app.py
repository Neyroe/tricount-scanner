"""
Point d'entrée desktop — lance le serveur FastAPI en arrière-plan
puis ouvre une fenêtre native via pywebview.
"""
import sys
import os
import threading
import time
import socket
import uvicorn

try:
    import webview
except ImportError as exc:  # pywebview absent ou backend indisponible
    print(
        "Fenêtre native indisponible (" + str(exc) + ").\n"
        "Installez pywebview (sous Windows : pip install pywebview pythonnet, "
        "avec le runtime WebView2), ou lancez l'application dans le navigateur :\n"
        "  python launch.py",
        file=sys.stderr,
    )
    raise SystemExit(1)

# Always run from the project directory so relative paths work
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from main import app as fastapi_app

PORT = 8765


def _port_ready(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _run_server():
    uvicorn.run(fastapi_app, host="127.0.0.1", port=PORT, log_level="error")


if __name__ == "__main__":
    # Start FastAPI in a daemon thread
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()

    # Wait until the server accepts connections (max 10s)
    if not _port_ready(PORT):
        print("Erreur : le serveur n'a pas démarré.", file=sys.stderr)
        sys.exit(1)

    window = webview.create_window(
        title="Tricount Split",
        url=f"http://127.0.0.1:{PORT}",
        width=960,
        height=780,
        min_size=(420, 600),
        background_color="#6C47FF",
    )
    webview.start()
