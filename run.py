import argparse
import os

from app import create_app

# ----- Modifiez ici le port par défaut -----
PORT_PAR_DEFAUT = 5003
# -------------------------------------------

app = create_app()
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lancer SIGF")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", PORT_PAR_DEFAUT)))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=True)
