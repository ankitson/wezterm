#!/usr/bin/env python3
"""
Local, deterministic reproduction of the WezTerm PaneFocused notification storm
(upstream issue #4390, fixed by PR #7763).

WHY LOCAL: the original incident was seen over SSHMUX (Mac GUI <-> Linux mux),
but the runaway is NOT remote-specific. PR #7763 problem #2 ("redundant
re-notification") makes every focus change emit MuxNotification::PaneFocused
*twice*; with two rapid switches the doubled events interleave and the reconcile
queue never drains. That reproduces on a single host with a plain unix domain,
no SSH and no second machine required.

WHAT IT DOES
  1. Spin up an isolated wezterm instance (own XDG_RUNTIME_DIR + config + unix
     domain) so it can't touch your real mux/sessions.
  2. Split into two panes in one tab.
  3. Drive rapid alternating `cli activate-pane` between the two panes at full
     local speed for a few seconds, then STOP all input.
  4. Measure the tell: with zero further input, does focus keep flipping on its
     own and does the GUI keep burning CPU? (queue-never-drains == the bug)

VERDICT
  RUNAWAY  -> self-sustained focus flips after input stops + climbing CPU (buggy)
  SETTLED  -> focus quiesces, CPU returns to idle (fixed)

USAGE
  # against the system/installed binaries (buggy baseline):
  ./local_repro.py

  # against a specific build (e.g. patched PR #7763):
  ./local_repro.py \
      --gui   /projects/external-repo/wezterm/target/debug/wezterm-gui \
      --mux   /projects/external-repo/wezterm/target/debug/wezterm-mux-server \
      --cli   /projects/external-repo/wezterm/target/debug/wezterm

Requires an X/Wayland display (DISPLAY/WAYLAND_DISPLAY) to open the GUI window.
"""
import argparse, json, os, shutil, signal, subprocess, sys, time

RUNDIR = "/tmp/wezrepro"
CONFIG = f"{RUNDIR}/wezterm.lua"
XRD    = f"{RUNDIR}/xrd"

CONFIG_LUA = """\
local wezterm = require 'wezterm'
local config = {}
-- unix domain; socket lives under XDG_RUNTIME_DIR (isolated per this instance)
config.unix_domains = { { name = 'repro' } }
config.font_size = 12.0
config.hide_tab_bar_if_only_one_tab = false
config.window_close_confirmation = 'NeverPrompt'
config.audible_bell = 'Disabled'
return config
"""


def env():
    e = dict(os.environ)
    e["XDG_RUNTIME_DIR"] = XRD
    e["WEZTERM_CONFIG_FILE"] = CONFIG
    e["WEZTERM_LOG"] = "info"
    e.setdefault("DISPLAY", ":0")
    return e


def setup():
    if os.path.isdir(XRD):
        shutil.rmtree(XRD, ignore_errors=True)
    os.makedirs(f"{XRD}/wezterm", exist_ok=True)
    # wezterm refuses a runtime dir that group/other can write to; lock both down
    os.chmod(XRD, 0o700)
    os.chmod(f"{XRD}/wezterm", 0o700)
    os.makedirs(RUNDIR, exist_ok=True)
    with open(CONFIG, "w") as f:
        f.write(CONFIG_LUA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gui", default=shutil.which("wezterm-gui") or "/usr/bin/wezterm-gui")
    ap.add_argument("--mux", default=shutil.which("wezterm-mux-server") or "/usr/bin/wezterm-mux-server")
    ap.add_argument("--cli", default=shutil.which("wezterm") or "/usr/bin/wezterm")
    ap.add_argument("--drive-secs", type=float, default=4.0, help="rapid-alternation drive duration")
    ap.add_argument("--watch-secs", type=float, default=12.0, help="post-input observation window")
    ap.add_argument("--keep", action="store_true", help="don't tear down the gui at the end")
    args = ap.parse_args()

    E = env()
    print(f"gui={args.gui}\nmux={args.mux}\ncli={args.cli}\n")

    def cli(*a, t=10):
        return subprocess.run([args.cli, "--config-file", CONFIG, *a],
                              capture_output=True, text=True, timeout=t, env=E)

    def panes():
        return json.loads(cli("cli", "list", "--format", "json").stdout or "[]")

    def focused():
        cl = json.loads(cli("cli", "list-clients", "--format", "json").stdout or "[]")
        return cl[0].get("focused_pane_id") if cl else None

    def cpu(pid):
        r = subprocess.run(["ps", "-o", "%cpu=", "-p", str(pid)], capture_output=True, text=True)
        return r.stdout.strip() or "?"

    setup()

    # launch the GUI (it spawns the isolated mux-server on demand via the unix domain)
    gui = subprocess.Popen([args.gui, "--config-file", CONFIG, "connect", "repro"],
                           env=E, stdout=open(f"{RUNDIR}/gui.out", "w"), stderr=subprocess.STDOUT,
                           start_new_session=True)
    print(f"launched gui pid {gui.pid}; waiting for it to attach...")
    deadline = time.monotonic() + 20
    ps = []
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            ps = panes()
        except Exception:
            ps = []
        if ps:
            break
    if not ps:
        print("ERROR: gui did not attach / no panes. gui.out:")
        print(open(f"{RUNDIR}/gui.out").read()[-800:])
        gui.terminate(); sys.exit(2)

    # ensure two panes in one tab
    if len(ps) < 2:
        cli("cli", "split-pane", "--pane-id", str(ps[0]["pane_id"]), "--right")
        time.sleep(1)
        ps = panes()
    ids = [p["pane_id"] for p in ps][:2]
    a, b = ids[0], ids[1]
    print(f"panes: {ids}  gui_cpu_baseline={cpu(gui.pid)}%")

    # PHASE 1: drive rapid alternation at full local speed
    print(f"\n=== PHASE 1: drive rapid activate-pane {a}<->{b} for {args.drive_secs}s ===")
    t0 = time.monotonic(); n = 0
    while time.monotonic() - t0 < args.drive_secs:
        cli("cli", "activate-pane", "--pane-id", str(a if n % 2 == 0 else b))
        n += 1
    print(f"issued {n} activate-pane calls; gui_cpu={cpu(gui.pid)}%")

    # PHASE 2: stop all input, observe self-sustain
    print(f"\n=== PHASE 2: STOP input. Observe self-sustained flips for {args.watch_secs}s ===")
    prev = None; flips = 0; t0 = time.monotonic(); nextlog = 0.0
    cpu_start = cpu(gui.pid)
    while time.monotonic() - t0 < args.watch_secs:
        f = focused()
        if f != prev:
            flips += 1; prev = f
        el = time.monotonic() - t0
        if el >= nextlog:
            print(f"  t={el:4.1f}s  gui_cpu={cpu(gui.pid):>5}%  flips_so_far={flips}")
            nextlog += 3
        time.sleep(0.05)

    verdict = "RUNAWAY (queue never drains -> BUG present)" if flips > 5 else "SETTLED (no self-sustain -> fixed)"
    print(f"\nself-sustained flips with zero input: {flips}")
    print(f"gui cpu  {cpu_start}% -> {cpu(gui.pid)}%")
    print(f"VERDICT: {verdict}")

    if not args.keep:
        print("\ntearing down gui...")
        try:
            os.killpg(os.getpgid(gui.pid), signal.SIGTERM)
        except Exception:
            gui.terminate()
    else:
        print(f"\n--keep: gui pid {gui.pid} left running (XDG_RUNTIME_DIR={XRD})")

    return 0 if flips > 5 else 1  # exit 0 when bug reproduced, 1 when settled/fixed


if __name__ == "__main__":
    sys.exit(main())
