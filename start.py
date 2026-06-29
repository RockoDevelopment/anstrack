#!/usr/bin/env python3
"""
ANSTRACK — startup
Boots the two main processes together:
  1) server.py            -> the web server (landing page at /, tracker at /ANSTRACK.html, JSON APIs)
  2) processor.py loop     -> the brain (scores launches, tracks devs/migrations, learns)

The other two scripts (launch/launch.py and launch/auto_lp_bot.py) are run manually
on Render when you actually launch the token — they are intentionally NOT started here.

Run locally:   py -3.11 start.py        (Windows)
               python3 start.py         (macOS/Linux)
On Render:     start command =  python start.py
               (server binds to $PORT automatically)

Ctrl+C stops both cleanly.
"""
import os, sys, subprocess, threading, time, signal

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"

# (label, command, restart_on_exit)
PROCS = [
    ("server", [PY, "server.py"], False),       # if the web server dies, bring everything down (Render restarts it)
    ("brain",  [PY, "processor.py", "loop"], True),  # the worker loop auto-restarts on a crash
]

_children = {}
_stop = threading.Event()


def _pump(label, proc):
    """Prefix each line of a child's output so logs are readable."""
    for raw in iter(proc.stdout.readline, b""):
        try:
            line = raw.decode("utf-8", "replace").rstrip()
        except Exception:
            line = str(raw)
        print(f"[{label}] {line}", flush=True)


def _spawn(label, cmd):
    p = subprocess.Popen(
        cmd, cwd=HERE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )
    _children[label] = p
    threading.Thread(target=_pump, args=(label, p), daemon=True).start()
    print(f"[start] {label} up (pid {p.pid}): {' '.join(cmd)}", flush=True)
    return p


def _shutdown(*_):
    if _stop.is_set():
        return
    _stop.set()
    print("\n[start] shutting down…", flush=True)
    for label, p in _children.items():
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    deadline = time.time() + 6
    for label, p in _children.items():
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    sys.exit(0)


def main():
    print("=" * 54)
    print("  ANSTRACK  ·  starting server + brain")
    port = os.getenv("PORT") or os.getenv("ANSTRACK_PORT") or "8787"
    print(f"  web        : http://localhost:{port}  (landing)")
    print(f"  tracker    : http://localhost:{port}/ANSTRACK.html")
    print("  stop       : Ctrl+C")
    print("=" * 54)

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)  # Render sends SIGTERM
    except Exception:
        pass

    restart = {}
    for label, cmd, _r in PROCS:
        _spawn(label, cmd)
        restart[label] = {"cmd": cmd, "auto": _r, "count": 0}

    # supervise
    while not _stop.is_set():
        time.sleep(1)
        for label, cfg in restart.items():
            p = _children.get(label)
            if p is None or p.poll() is None:
                continue
            code = p.returncode
            if cfg["auto"] and cfg["count"] < 20 and not _stop.is_set():
                cfg["count"] += 1
                print(f"[start] {label} exited (code {code}) — restarting ({cfg['count']}/20)", flush=True)
                time.sleep(3)
                _spawn(label, cfg["cmd"])
            else:
                print(f"[start] {label} exited (code {code}) — stopping everything", flush=True)
                _shutdown()


if __name__ == "__main__":
    main()