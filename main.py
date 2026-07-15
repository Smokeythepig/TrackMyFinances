import threading
import time
import webview
from database import init_db
from server import app, do_refresh

PORT = 7432
AUTO_REFRESH_HOURS = 6


def start_flask():
    # localhost only — never expose financial data on the network
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


def auto_refresh_loop():
    while True:
        time.sleep(AUTO_REFRESH_HOURS * 3600)
        try:
            do_refresh()
        except Exception:
            pass  # network hiccups shouldn't kill the loop


def main():
    init_db()

    t = threading.Thread(target=start_flask, daemon=True)
    t.start()
    time.sleep(0.8)  # let Flask bind before opening the window

    threading.Thread(target=auto_refresh_loop, daemon=True).start()

    window = webview.create_window(
        "TrackMyFinances",
        f"http://127.0.0.1:{PORT}",
        width=1280,
        height=820,
        min_size=(900, 600),
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
