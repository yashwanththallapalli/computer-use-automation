"""Entry point for the mock bank app.

    python run_bankapp.py                  # serve on http://127.0.0.1:5001
    BANKAPP_PORT=5002 python run_bankapp.py # or another port (tests use this
                                             # to run a second instance)
"""

import os

from bankapp import create_app

if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("BANKAPP_PORT", "5001"))
    app.run(host="127.0.0.1", port=port, debug=False)
