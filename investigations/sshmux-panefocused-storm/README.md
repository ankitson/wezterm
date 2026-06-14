# WezTerm SSHMUX "PaneFocused" flip/storm loop — diagnosis & resolution

**Date:** 2026-06-13
**Status:** Root-caused, live incident resolved, **reproduced deterministically, and PR #7763 verified to fix it.**
**WezTerm build (Mac GUI + Linux mux `desktop-linux`):** `20260117-154428-05343b38`

## Symptom

On connecting to the SSHMUX domain, the WezTerm GUI rapidly oscillates the active
tab/pane between two panes ("rapidly switching between titles/panes in one of the
tabs"). One CPU core is pinned, input becomes sluggish, and it recurs on essentially
every SSHMUX (re)connect. Originally reported as a per-second title-spinner flicker,
but the real failure is a high-frequency focus loop (thousands/sec).

## Root cause

A WezTerm GUI-frontend bug — upstream issue **#4390**, fixed by **PR #7763**
("fix/panefocused-reconcile-storm", commit `44a8f93`). Three compounding problems:

1. **Destroyed-pane storm.** When a (unix/SSH) domain detaches, the mux tears down
   many panes and emits a burst of `PaneFocused` notifications for pane IDs that no
   longer exist. The GUI handler spawns a **main-thread task per notification** with
   no existence check, so it queues thousands of tasks faster than the UI thread can
   drain them — each failing with `pane N not found`.
2. **Redundant re-notification.** `Mux::add_pane_to_window`, `sessionhandler`, and
   `tmux_commands` each fire duplicate `PaneFocused` events, doubling the storm during
   rapid switches.
3. **Client echo-back (makes it perpetual).** A remote `PaneFocused` notification
   triggers `focus_changed(true)` on the client pane, which echoes a fresh
   `SetFocusedPane` PDU back to the server, which re-broadcasts `PaneFocused` … an
   infinite feedback loop between GUI and mux. This is why it never self-terminated
   and why it survived disconnecting the mux.

### The buggy code

`wezterm-gui/src/frontend.rs` (this build, ~line 80):

```rust
MuxNotification::PaneFocused(pane_id) => {
    promise::spawn::spawn_into_main_thread(async move {
        let mux = Mux::get();
        if let Err(err) = mux.focus_pane_and_containing_tab(pane_id) {
            log::error!("Error reconciling PaneFocused notification: {err:#}");
        }
    })
    .detach();
}
```

No `mux.get_pane(pane_id)` guard before spawning → every stale notification costs a
full main-thread task. PR #7763 adds a synchronous existence check here, plus a
`NotifyMux::No` mechanism (`mux/src/tab.rs`) to suppress the duplicate notifications,
and pre-sets `focused_remote_pane_id` in `wezterm-client/src/pane/clientpane.rs` to
kill the echo-back.

### Confirmed trigger sequence (Mac GUI log)

`~/.local/share/wezterm/wezterm-gui-log-<gui-pid>.txt`:

```
13:38:29  ERROR wezterm_client::pane::renderable > get_lines failed: send_pdu send   (mux link breaking)
13:38:34.466 INFO wezterm_client::domain > detached domain 3
13:38:34.477 INFO mux > domain detached panes: [48,49,45,50,52,38,51,40,41,47,43,42,39,46,44]
13:38:34.656 ERROR wezterm_gui::frontend > Error reconciling PaneFocused notification: pane 46 not found
            … 149,989+ errors, alternating pane 46 / 47, ~262 KB/s …
```

(46 and 47 are the GUI's *local* pane IDs for two of the detached SSHMUX panes. An
earlier detach in the same log produced the same loop on local panes 13/17 — which is
the 13⇄17 flip observed while attached. The specific IDs depend on which detach left a
notification in flight.)

## How it was diagnosed (what was ruled OUT)

All via reversible `SIGSTOP`/`SIGCONT` and live sampling of `focused_pane_id`:

| Suspect | Test | Result |
|---|---|---|
| Title-spinner animation | trace, panes idle | not it |
| Codex desktop bridge (Linux) + remote-control app-server | SIGSTOP all | no effect |
| Mac Codex.app full tree + Computer-Use service | SIGSTOP all | no effect |
| In-pane `codex` TUI (pane 17) | SIGSTOP | no effect |
| Pane output flood / OSC 10-11 queries | panes idle | no effect |
| Split geometry | moved pane to own tab | flip followed the pane |
| `wezterm cli activate-pane` driving it | 535 /proc scans/s (Linux) + tight ps (Mac) | zero |
| Config hooks (both hosts) | read configs | none activate/focus |
| User input / mouse | client `idle_time` 106 s+ | not it |
| Mux connection itself | killed Linux proxy | **flip continued on Mac** → driver is GUI-internal |

The decisive evidence was the Mac GUI log: it was the only thing that pointed at the
frontend reconcile path. **Lesson:** the on-server `wezterm-trace` never captured the
*Mac* GUI log, which is where the smoking gun lived.

## Resolution (live incident)

The poisoned event queue lives in the `wezterm-gui` process and cannot be drained;
disconnecting/reconnecting the mux does **not** clear it (and re-triggers it). The
only fix is to restart the GUI:

1. Backed up full server-side mux state (all panes + scrollback):
   `/home/ankit/wezterm_state_backups/wezterm-state-20260613-133417.json` (+ `_pane_text/`).
2. `ssh m2book 'kill <gui-pid>'` (was pinned at **98.8% CPU**), then
   `ssh m2book 'open -a WezTerm'`.
3. New GUI: **0% CPU, 0 reconcile errors**. All **16 panes/sessions survived** on the
   Linux mux and the GUI reattached with normal focus behavior.

## Recovery runbook (for next time)

1. Confirm: on the Mac, `tail -f ~/.local/share/wezterm/wezterm-gui-log-<pid>.txt` —
   look for `Error reconciling PaneFocused notification: pane N not found` flooding,
   and the `wezterm-gui` process near 100% CPU. `wezterm cli` will hang.
2. (Optional) back up server state — see tooling below.
3. `kill <gui-pid>` on the Mac, then `open -a WezTerm`. Sessions persist on the mux.

## Permanent fix options

- **Upgrade WezTerm** to a build containing PR #7763 once merged, or
- **Build a patched WezTerm** from `fculpo:fix/panefocused-reconcile-storm` (or
  cherry-pick the frontend `get_pane` guard) for the Mac GUI **and** the Linux mux.

## Artifacts & paths

- Evidence writeup: `/home/ankit/wezterm_traces/ROOT-CAUSE-paneFocused-loop-20260613.md`
- Server-state backup: `/home/ankit/wezterm_state_backups/wezterm-state-20260613-133417.json`
- Incident trace bundle: `/home/ankit/wezterm_traces/wezterm-trace-20260613-131436.tar.gz`
- Capture tooling: `~/toolbox/bin/wezterm-trace`, `~/toolbox/bin/wezterm-trace-summarize`
- WezTerm source: Linux `/projects/external-repo/wezterm`; Mac `/Users/ankit/Documents/wezterm`
- Upstream: issue #4390, PR https://github.com/wezterm/wezterm/pull/7763

## Stable reproduction (DONE)

The runaway is **not** remote-specific. The self-sustaining part is problem #2
(*redundant re-notification*): every focus change fires `PaneFocused` twice, so two
rapid switches interleave and the reconcile queue never drains. That reproduces on a
**single host** with a plain unix domain — no SSH, no second machine, no Codex.

Harness: `repro/local_repro.py` (see `repro/README.md`). It spins up a fully isolated
wezterm instance (own `XDG_RUNTIME_DIR` + throwaway `repro` unix domain, cannot touch
the real mux), splits into two panes, drives ~370 rapid alternating `cli activate-pane`
calls, then **stops all input** and measures whether focus keeps flipping on its own and
whether GUI CPU keeps climbing.

- **Tell of the bug:** with zero input, focus keeps changing and GUI CPU climbs
  monotonically and never settles ("queue never drains").
- Installed build `20260117-…-05343b38`: **112 self-sustained flips / 15 s, CPU 39→50 %.**

## Fix verification (DONE) — PR #7763

Built both sides of the PR from the same source tree (`/projects/external-repo/wezterm`),
same flags (`--no-default-features --features vendored-fonts` debug; X11), so the **only**
difference is the one fix commit `44a8f937`. Ran the *identical* `local_repro.py` against
each:

| Build (same tree, same flags) | self-sustained flips (zero input, 12 s) | GUI CPU | Verdict |
|---|---|---|---|
| Parent `577474d8` — **BUGGY** | **92** | 60 % → **102 %** (pins a core) | RUNAWAY |
| PR head `44a8f937` — **FIXED** | **1** (final settle) | 12 % → **4.8 %** (idle) | SETTLED |

**Conclusion:** PR #7763 conclusively fixes the storm. With the patch, focus quiesces the
instant input stops and CPU returns to idle; without it (parent commit, identical build)
the loop self-sustains and pins a full core.

### Build notes (Linux, for re-running)

- Needs WezTerm's X11/XCB dev libs (the packaged binary ships without dev headers). One-time:
  `sudo apt-get install -y libegl1-mesa-dev libssl-dev libfontconfig1-dev libwayland-dev
  libx11-xcb-dev libxcb-ewmh-dev libxcb-icccm4-dev libxcb-image0-dev libxcb-keysyms1-dev
  libxcb-randr0-dev libxcb-render0-dev libxcb-xkb-dev libxkbcommon-dev libxkbcommon-x11-dev
  libxcb-util0-dev xorg-dev cmake` (from the repo's `get-deps`).
- `git checkout 44a8f937b` (PR head) / `577474d89` (parent) then
  `cargo build -p wezterm-gui --no-default-features --features vendored-fonts -p wezterm-mux-server`.
  Both gui (frontend, #1/#3) and mux-server (sessionhandler, #2) carry the fix, so both must
  be built and swapped together.
- The runtime dir must be `0700` (`XDG_RUNTIME_DIR` **and** its `wezterm/` subdir) or the
  mux-server refuses to bind.

## Permanent fix (for the Mac — the real environment)

The verified fix is upstream PR #7763. For the actual day-to-day setup, the Mac `wezterm-gui`
**and** the Linux `wezterm-mux-server` both need a build containing it (build on each host
from `44a8f937b`, or wait for it to merge and upgrade). Until then, the recovery runbook above
(restart the Mac GUI) clears each occurrence.
