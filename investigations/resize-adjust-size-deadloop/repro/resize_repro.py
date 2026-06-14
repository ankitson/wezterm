#!/usr/bin/env python3
"""
Reproduce the adjust_x_size/adjust_y_size dead loop (wezterm issue #7765).

The loop lives in the GUI's resize handler:
  set_window_size -> apply_dimensions -> Tab::resize -> adjust_x_size(...)
When a resize asks a horizontal split tree to shrink below 1 cell/pane, the
shrink branch makes no progress but the while loop keeps going -> infinite loop
on the GUI main thread -> window freezes, one core pinned at 100%.

Strategy: make many side-by-side panes, then progressively shrink the window
width with xdotool until a resize demands sub-minimum width. Detect the dead
loop by sampling the GUI process CPU jiffies (a stuck loop burns ~1 core
continuously and never settles), and by the window no longer accepting resizes.

Run against a chosen build:
  ./resize_repro.py --gui .../wezterm-gui --mux .../wezterm-mux-server --cli /usr/bin/wezterm
"""
import argparse, json, os, shutil, signal, subprocess, sys, time

RUNDIR = "/tmp/wezrepro"
CONFIG = f"{RUNDIR}/wezterm.lua"
XRD = f"{RUNDIR}/xrd"
CLK_TCK = os.sysconf("SC_CLK_TCK")

CONFIG_LUA = """\
local wezterm = require 'wezterm'
local config = {}
config.unix_domains = { { name = 'repro' } }
config.font_size = 12.0
config.hide_tab_bar_if_only_one_tab = false
config.window_close_confirmation = 'NeverPrompt'
config.audible_bell = 'Disabled'
-- keep initial window roomy so we have splits to shrink
config.initial_cols = 200
config.initial_rows = 50
return config
"""


def env():
    e = dict(os.environ)
    e["XDG_RUNTIME_DIR"] = XRD
    e["WEZTERM_CONFIG_FILE"] = CONFIG
    e["WEZTERM_LOG"] = "error"
    e.setdefault("DISPLAY", ":0")
    return e


def setup():
    shutil.rmtree(XRD, ignore_errors=True)
    os.makedirs(f"{XRD}/wezterm", exist_ok=True)
    os.chmod(XRD, 0o700)
    os.chmod(f"{XRD}/wezterm", 0o700)
    os.makedirs(RUNDIR, exist_ok=True)
    with open(CONFIG, "w") as f:
        f.write(CONFIG_LUA)


def cpu_jiffies(pid):
    """utime+stime in jiffies for the whole process."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        return int(parts[13]) + int(parts[14])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gui", default="/usr/bin/wezterm-gui")
    ap.add_argument("--mux", default="/usr/bin/wezterm-mux-server")
    ap.add_argument("--cli", default="/usr/bin/wezterm")
    ap.add_argument("--splits", type=int, default=20, help="side-by-side panes to create")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    E = env()
    print(f"gui={args.gui}\nsplits={args.splits}\n")

    def cli(*a, t=8):
        return subprocess.run([args.cli, "--config-file", CONFIG, *a],
                              capture_output=True, text=True, timeout=t, env=E)

    def panes():
        return json.loads(cli("cli", "list", "--format", "json").stdout or "[]")

    setup()
    gui = subprocess.Popen([args.gui, "--config-file", CONFIG, "connect", "repro"],
                           env=E, stdout=open(f"{RUNDIR}/gui.out", "w"),
                           stderr=subprocess.STDOUT, start_new_session=True)
    print(f"gui pid {gui.pid}; waiting to attach...")
    ps = []
    for _ in range(20):
        time.sleep(1)
        try:
            ps = panes()
        except Exception:
            ps = []
        if ps:
            break
    if not ps:
        print("ERROR: gui did not attach.\n" + open(f"{RUNDIR}/gui.out").read()[-600:])
        gui.terminate(); return 2

    # build a row of side-by-side panes
    made = 1
    while made < args.splits:
        cur = panes()
        # split the widest pane to keep going
        widest = max(cur, key=lambda p: p.get("size", {}).get("cols", 0))
        r = cli("cli", "split-pane", "--pane-id", str(widest["pane_id"]), "--right")
        if r.returncode != 0:
            print(f"  split #{made+1} refused (panes too small): {r.stderr.strip()[:80]}")
            break
        made += 1
    cur = panes()
    cols = sorted(p.get("size", {}).get("cols") for p in cur)
    print(f"created {len(cur)} side-by-side panes; pane widths(cols)={cols}")

    # find the X11 window
    wid = subprocess.run(["xdotool", "search", "--pid", str(gui.pid)],
                         capture_output=True, text=True, env=E).stdout.split()
    wid = wid[-1] if wid else None
    if not wid:
        # fall back: most-recently-active window
        wid = subprocess.run(["xdotool", "search", "--name", "."],
                             capture_output=True, text=True, env=E).stdout.split()
        wid = wid[-1] if wid else None
    print(f"window id: {wid}")
    geo = subprocess.run(["xdotool", "getwindowgeometry", wid], capture_output=True, text=True, env=E).stdout if wid else ""
    print("initial geometry:", geo.replace("\n", " "))

    def gui_cpu_pct(secs=1.0):
        a = cpu_jiffies(gui.pid)
        time.sleep(secs)
        b = cpu_jiffies(gui.pid)
        if a is None or b is None:
            return None
        return 100.0 * (b - a) / (CLK_TCK * secs)

    print(f"\nbaseline GUI cpu: {gui_cpu_pct():.0f}% of one core")

    # progressively shrink the window WIDTH; after each, check for a stuck loop
    print("\n=== shrinking window width and watching for a stuck 100% loop ===")
    for w in [1200, 800, 600, 400, 300, 200, 150, 120, 100, 80, 60, 40]:
        if not wid:
            break
        subprocess.run(["xdotool", "windowsize", wid, str(w), "700"], env=E,
                       capture_output=True, text=True)
        # let the resize event be processed, then measure sustained CPU
        pct = gui_cpu_pct(1.2)
        # responsiveness: does a fresh xdotool size query round-trip? (X server still serves;
        # the tell is sustained CPU, so sample twice)
        pct2 = gui_cpu_pct(1.2)
        stuck = pct is not None and pct > 80 and pct2 is not None and pct2 > 80
        print(f"  width={w:>5}px  gui_cpu={pct:5.0f}% then {pct2:5.0f}%  {'<-- STUCK (dead loop)' if stuck else ''}")
        if stuck:
            print("\nDEAD LOOP REPRODUCED: GUI main thread pinned ~100% and not settling after resize.")
            # prove it stays stuck for a few more seconds with no further input
            for _ in range(3):
                p = gui_cpu_pct(1.0)
                print(f"    still stuck: gui_cpu={p:.0f}%")
            if not args.keep:
                try:
                    os.killpg(os.getpgid(gui.pid), signal.SIGKILL)
                except Exception:
                    gui.kill()
            return 0
    print("\nNo sustained loop observed across the shrink sweep.")
    if not args.keep:
        try:
            os.killpg(os.getpgid(gui.pid), signal.SIGTERM)
        except Exception:
            gui.terminate()
    return 1


if __name__ == "__main__":
    sys.exit(main())
