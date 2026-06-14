# WezTerm mux resize-sync issues: attach undersize (#5117) + drag desync (#5142)

**Date:** 2026-06-13
**Status:** Problem 1 (attach/activate undersize) — fix implemented, built, deployed to the
Mac; awaiting clean user verification. Problem 2 (drag desync) — root-caused, tractability
assessed, **no code change yet** (per request).

Distinct from the resize *dead-loop* (`../2026-06-13-wezterm-resize-adjust-size-deadloop/`,
issue #7765). These are about size *propagation*, not an infinite loop.

## Problem 1 — pane not full-width until you nudge the window (issue #5117)

### Symptom
On a SSHMUX session, a mux tab can render narrower (or shorter) than the window; manually
nudging the window size fixes it. Observed live: one window held three different widths
(local 137 cols, mux tabs 135 and 99) — the mux tabs weren't re-fit to the window. (The
99-col one was literally created by a `cli spawn`, which sizes to the mux default, not the
window.)

### Root cause
`TermWindow::activate_tab` (`wezterm-gui/src/termwindow/mod.rs`) sets the active tab and
calls `focus_changed` / `update_title`, but **never resizes the tab to the window**. The
only place panes are re-fit is `apply_dimensions` (`resize.rs:298`), which runs on an actual
OS resize event:

```rust
for tab in window.iter() { tab.resize(size); }   // only on a real window resize
```

Local tabs are always created at the window size and kept in sync by every resize, so this is
invisible for them. A **mux/client** tab can carry an independent, stale size (set by the
server, another attached client, a detach/reattach, or `cli spawn`); activating it doesn't
re-fit it, so it stays wrong until the next OS resize nudges `apply_dimensions`.

### Fix (deployed)
Re-fit the activated tab to the window inside `activate_tab`, but only when the grid actually
differs (so a normal tab switch doesn't emit a redundant Resize PDU / `TabResized`):

```rust
if let Some(tab) = mux.get_active_tab_for_window(self.mux_window_id) {
    let cur = tab.get_size();
    if cur.rows != self.terminal_size.rows || cur.cols != self.terminal_size.cols {
        tab.resize(self.terminal_size);
    }
}
```

Covers the keyboard (`activate_tab_relative`) and mouse activation paths (both route through
`activate_tab`). The `wezterm cli activate-tab` path is separate (server-side, `main.rs`) and
has no window size to apply, so it's left alone.

Compile-checked on Linux; built release on the Mac and swapped into
`/Applications/WezTerm.app` (re-signed ad-hoc). **Verify:** switch to a previously mis-sized
mux tab via keyboard/click — it should snap to full width without nudging the window.

## Problem 2 — terminal width desyncs from the window while dragging (issues #5142 / #3694)

### Symptom
While dragging the window (or a split divider) the terminal grid lags the window size; panes
"dance" and keep adjusting for a moment after the mouse is released; resize feels slow,
especially with content / over SSH.

### Root cause (confirmed in code)
There is **no throttling/coalescing of live-resize events**. The chain per OS resize event:

```
WindowEvent::Resized{live_resizing} (mod.rs:953)
  -> TermWindow::resize (resize.rs:23) — only NOPs if pixel dims are identical
  -> apply_dimensions (resize.rs:62, called per-event during live resize)
       -> for tab in window: tab.resize(size)
            -> (mux/client tab) sends an async Resize PDU to the server
            -> fires MuxNotification::TabResized
                 -> GUI handler calls update_title_post_status()  (mod.rs:1304)
```

During a drag the OS emits many resize events/sec; each one re-resizes every tab, sends a
Resize PDU per mux pane, and fires a `TabResized` → `update_title_post_status` per tab. The
local grid follows the window instantly, but mux pane *content* reflects the server's
processing of the queued Resize PDUs, which lags → the visible width desync. After release the
PDU/`TabResized` backlog drains → the "dancing." (Community profiling on #5142 independently
fingered `update_title_impl` as a hotspot and noted `TabResized` firing far more than needed,
including after mouse-up.)

### Tractability
Partial. Three tiers, increasing impact/risk:

1. **Low-risk, partial:** debounce `update_title_post_status` on `TabResized` (coalesce a burst
   into one deferred update). Kills the CPU hotspot / much of the slowness, but **not** the
   core desync — the PDU round-trip lag remains.
2. **The real fix, medium-risk:** coalesce live-resize propagation. Keep the local grid
   responsive, but only send mux Resize PDUs + fire the heavy notifications on a short trailing
   timer (e.g. ~30–50 ms quiet, plus a guaranteed final on release). Needs a pending-resize
   mechanism in `TermWindow` and careful handling so the final size always lands. Requires live
   testing on the actual mux session to tune — hard to do non-intrusively.
3. **Hardest:** the post-release "dancing" (server echoing resize acks that re-trigger). Root
   cause not fully established upstream; needs live tracing.

This is why #5142 has stayed open: tier-2 is the fix that matters and it's a non-trivial,
risky change that wants real mux-session testing.

## Links
- #5117 — some panes don't resize properly when reattaching to a domain (Problem 1)
- #5142 — resizing in mux domains has issues (Problem 2)
- #3694 — resizing perf / incorrect dimension calc with mux server (Problem 2)
