# PR #7763 — responses to @bew's review

Reference doc for the review on <https://github.com/wezterm/wezterm/pull/7763>
("Fix PaneFocused notification storm on pane destruction and mux reconciliation").
Maps each of @bew's 10 inline comments to either a code change (made on branch
`fix/panefocused-reconcile-storm` in `ankitson/wezterm`) or a written answer here.

**Context from the reviewer:** @bew notes upstream WezTerm is effectively on hold
(Wez not responding to PRs) and that they intend to start a community fork
(see wezterm#6341). So the practical merge target is likely that fork. The changes
below are written to stand on their own there or upstream.

Our independent reproduction + fix verification (which back several answers below)
live in the sibling docs:
- `README.md` (this folder) — PaneFocused storm: root cause, local repro, A/B verification
- `../2026-06-13-wezterm-resize-adjust-size-deadloop/` — the related resize dead-loop

---

## Code changes (made on the branch)

### 3. `clientpane.rs:234` — "please link to the issue for reference"
Added `// See https://github.com/wezterm/wezterm/issues/4390` to the echo-back guard comment.

### 4 + 1. `tab.rs` test — "add comments about the phases" / "`seen` is set but never read"
`set_active_pane_can_suppress_mux_notification`:
- Added phase comments (arrange subscriber → build a 2-pane split → switch with
  `NotifyMux::No` → assert no `PaneFocused` emitted but active pane still changed).
- Clarified the `seen`/`notifications` pair: they are **the same** `Arc<Mutex<Vec>>`.
  `seen` is the clone *moved into* the subscriber callback (which needs to own it);
  `notifications` is the original handle read by the assertion. Renamed `seen` →
  `notified_panes` and added a comment so the writer/reader relationship is obvious.
  (So it *is* read — via the shared Vec — but the original naming hid that.)

### 5. `mux/src/lib.rs:562` — "comment why we don't notify mux here"
### 7. `sessionhandler.rs:361` — same
### 8. `tmux_commands.rs:344` — same
Added a short comment at each `NotifyMux::No` site explaining that the caller
**immediately** fires its own `MuxNotification::PaneFocused` (or, for `lib.rs`, that
the activation is internal reconciliation that must not re-broadcast), so letting
`advise_focus_change` also notify would double the event — the exact doubling that
fuels the storm (problem #2). Each comment links wezterm#4390.

### 6. `tab.rs:1785` — "rename `notify` to `notify_mux`"
Renamed the parameter `notify: NotifyMux` → `notify_mux: NotifyMux` in
`set_active_pane_with_notify`, the inner `set_active_pane`, and `advise_focus_change`,
so reading the call sites makes the intent explicit.

---

## Written answers (design / root-cause questions)

### 2. `frontend.rs:86` — "users got `pane X not found` in #4390 but weren't destroying panes — why?"

This is the key question, and our reproduction answers it. **The `get_pane` guard
here is defense-in-depth for *stale* notifications; it is not the part that fixes
#4390 for users who aren't destroying panes.** Two distinct things produce the
symptoms:

1. **The `pane not found` log line** fires whenever a `PaneFocused(id)` is reconciled
   after `id` left the mux. Domain detach (our SSHMUX case) is the most dramatic
   source — a burst of notifications for dozens of just-destroyed panes — but it is
   **not the only** one. Any pane-lifecycle race produces it: closing a pane
   (`CloseCurrentPane`) or replacing one during a split while a focus notification is
   already queued, and — in the mux/client case — transient client-vs-server pane-id
   reconciliation. The guard drops all of these cheaply instead of spawning a
   doomed main-thread task per event. So a #4390 user who never "destroys" a pane can
   still hit it via ordinary close/split/activate-direction timing.

2. **The CPU-pinning storm itself** (the part that makes it *unrecoverable*, not just
   noisy) is **not** about missing panes at all — it is the doubled notification
   (problem #2) plus the client echo-back (problem #3) forming a self-sustaining
   reconcile loop. We reproduced this **with no pane destruction whatsoever**: on a
   plain unix domain, two panes, rapid alternating `wezterm cli activate-pane`, then
   *stop all input* — focus keeps flipping on its own and a core pins at ~100%.
   Same-tree A/B (only this PR's commit differs):

   | build | self-sustained focus flips after input stops (12s) | GUI CPU |
   |---|---|---|
   | parent (buggy) | 92 | 60% → 102% (pinned) |
   | this PR | 1 (settles) | 12% → 4.8% (idle) |

   That is exactly #4390's "switching panes in multiple directions ... rapidly
   switch when any key is pressed," reproduced deterministically, and it is fixed by
   #2 + #3 — independent of the `get_pane` guard.

   So the three sub-fixes are complementary: **#1 (guard)** stops stale notifications
   from flooding the UI thread (the `pane not found` log line); **#2 (no double-fire)**
   and **#3 (no echo-back)** stop the loop from sustaining itself. #4390 users see the
   log line from (1)'s sources, but their *freeze* is (2)+(3).

### 9. `Cargo.toml:61` — "what is the chrono `clock` feature change for?"

**Required — not arbitrary, and worth keeping.** `mux/src/client.rs` calls
`Utc::now()` at lines 61/63/69 (`ClientInfo::connected_at` / `last_input`), which needs
chrono's `clock` feature. The workspace chrono dep is declared
`default-features=false` *without* `clock`; the full-app build compiled anyway only
because another crate in the graph enabled `clock` and Cargo unifies features across a
workspace build. This PR adds a `mux` **unit test**, which makes `cargo test -p mux`
(building the `mux` lib test in isolation) a real configuration — and in *that* graph
nothing else turns on `clock`, so `mux` fails to compile:

```
error[E0599]: no function or associated item named `now` found for struct `Utc`
  --> mux/src/client.rs:61:32
```

Adding `clock` to the workspace chrono dep makes `mux` build standalone, which the new
test requires. Verified empirically: deleting `"clock"` and running
`cargo build -p mux --tests` reproduces the E0599 above; restoring it builds clean. So
the line belongs with this PR (it's a prerequisite of the test it ships), and a comment
to that effect would answer this for the next reader.

### 10. `tab.rs:707` — "remove `set_active_pane` entirely (or add `notify_mux` to it) so callers must think about notifications"

There are only **3** external callers of the convenience wrapper today:
`lua-api-crates/mux/src/pane.rs` (Lua `ActivatePane`) and two in
`sessionhandler.rs` (set-zoomed activations). All three are **genuine
user-initiated activations that *should* notify the mux** — i.e. they legitimately
want `NotifyMux::Yes`.

Recommendation (and what the branch does): **keep** the `set_active_pane(pane)`
wrapper as the explicit "yes, notify" entry point, but document it as such, and reserve
`set_active_pane_with_notify(pane, NotifyMux::No)` for the suppression sites that
fire their own notification. Rationale: forcing every caller to write
`NotifyMux::Yes` adds ceremony to the common, correct default and risks a future
caller copy-pasting `NotifyMux::No` without the matching manual `mux.notify(...)` —
re-introducing a silent focus-desync. The danger we are guarding against is
*accidental double-notify*, and those sites are now all explicit and commented.

If the maintainer prefers maximum explicitness, the alternative is a 3-caller change:
delete the wrapper and pass `NotifyMux::Yes` at each. Happy to do that instead — it
is mechanical. Flagging the trade-off rather than silently picking the more invasive
option.

---

## Summary

| # | File | Comment | Disposition |
|---|------|---------|-------------|
| 1 | tab.rs:2551 | `seen` set but not read | code: rename + comment (it *is* read via shared Vec) |
| 2 | frontend.rs:86 | pane-not-found without destroying panes? | answered above (guard = stale-notif defense; storm = #2+#3) |
| 3 | clientpane.rs:234 | link the issue | code: added #4390 link |
| 4 | tab.rs:2535 | comment the test phases | code: added phase comments |
| 5 | lib.rs:562 | why no notify here | code: added comment |
| 6 | tab.rs:1785 | rename `notify`→`notify_mux` | code: renamed |
| 7 | sessionhandler.rs:361 | why no notify here | code: added comment |
| 8 | tmux_commands.rs:344 | why no notify here | code: added comment |
| 9 | Cargo.toml:61 | chrono `clock` — what for? | see verdict above |
| 10 | tab.rs:707 | remove `set_active_pane` wrapper | answered: keep + document; alt offered |
