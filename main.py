"""
SPX Options Trading Bot - Main Entry Point
"""
import tkinter as tk
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# تفعيل DPI awareness على Windows حتى لا تتصغّر النافذة عند scaling > 100%
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from ui.app import SPXBotApp


def _run_sync():
    """Run legacy sync only when explicitly enabled in Settings."""
    try:
        from core.database import get_setting
        enabled = str(get_setting("enable_auto_sync_on_exit", "0") or "0").lower() in ("1", "true", "yes", "on")
    except Exception:
        enabled = False
    if not enabled:
        print("[sync] automatic legacy sync disabled")
        return

    import subprocess
    sync_bat = os.path.join(os.path.dirname(__file__), "sync.bat")
    if os.path.exists(sync_bat):
        try:
            # RC15i: bounded sync. Auto-sync is disabled by default, but if enabled
            # it must not leave an unbounded child process during UI shutdown.
            subprocess.run(["cmd", "/c", sync_bat], timeout=20, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            print("[sync] synchronization finished or exited")
        except subprocess.TimeoutExpired:
            print("[sync] skipped: sync timeout after 20s")
        except Exception as e:
            print(f"[sync] error: {e}")


def main():
    root = tk.Tk()
    app = SPXBotApp(root)

    # مزامنة تلقائية عند إغلاق البوت
    def _on_close():
        # RC15i.2: stop scheduled UI/API refresh callbacks before destroying Tk.
        try:
            app._closing = True
        except Exception:
            pass
        _run_sync()
        try:
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
