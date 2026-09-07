"""
Lanceur multiplateforme (Windows, macOS, Linux).

Crée le .env au premier démarrage, affiche les URL d'accès (locale + réseau
local, pour ouvrir l'appli depuis le téléphone) puis démarre le serveur.

    python launch.py            # Linux / macOS
    py -3 launch.py             # Windows
"""
import os
import shutil
import socket
import sys
import webbrowser
from pathlib import Path

# En exécutable packagé, .env et credentials.json vivent à côté de l'exe.
_FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).parent if _FROZEN else Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "8000"))


def ensure_env() -> bool:
    """Crée .env depuis .env.example au premier lancement. False si à compléter."""
    env = ROOT / ".env"
    if env.exists():
        return True
    example = ROOT / ".env.example"
    if not example.exists():  # exécutable packagé : pas de fichier d'exemple à côté
        example = Path(getattr(sys, "_MEIPASS", ROOT)) / ".env.example"
    if example.exists():
        shutil.copyfile(example, env)
    else:
        env.write_text("tricount_url=\n", encoding="utf-8")
    print(f"\n  ⚠️  Fichier .env créé : {env}")
    print("      Collez-y le lien de partage de votre Tricount, puis relancez.\n")
    return False


def lan_ip() -> str:
    """Adresse IP de la machine sur le réseau local (aucun paquet n'est envoyé)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))  # UDP : ouvre juste une route, ne transmet rien
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    os.chdir(ROOT)
    if not ensure_env():
        return 1

    try:
        import uvicorn
    except ImportError:
        print("\n  ❌  Dépendances manquantes. Lancez d'abord :")
        print(f"      {Path(sys.executable).name} -m pip install -r requirements.txt\n")
        return 1

    ip = lan_ip()
    print("\n  🧾  Tricount Scanner")
    print("  ─────────────────────────────────────")
    print(f"  Local   : http://localhost:{PORT}")
    print(f"  Mobile  : http://{ip}:{PORT}")
    print("  ─────────────────────────────────────")
    print("  (Les deux appareils doivent être sur le même Wi-Fi)")
    print("  Ctrl+C pour arrêter.\n")

    if os.getenv("NO_BROWSER") != "1":
        webbrowser.open(f"http://localhost:{PORT}")

    from main import app  # importé après le contrôle des dépendances

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
