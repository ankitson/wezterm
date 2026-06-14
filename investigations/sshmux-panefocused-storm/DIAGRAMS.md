# PaneFocused storm — event/notification flow across the client↔server boundary

Sequence diagrams of the focus-notification protocol between the **client**
(`wezterm-gui`, on the Mac) and the **server** (`wezterm-mux-server`, on Linux),
over the SSHMUX connection — before and after PR #7763.

Two PDUs cross the boundary:

- **`SetFocusedPane(id)`** — client → server: "the user focused this pane here."
  Sent by `ClientPane::advise_focus` (`wezterm-client/src/pane/clientpane.rs:579`),
  **only if** `focused_remote_pane_id != id`.
- **`PaneFocused(id)`** — server → client: a `MuxNotification` broadcast, converted to a
  PDU in `wezterm-mux-server-impl/src/dispatch.rs:169`. The client's frontend reacts by
  spawning a main-thread reconcile task (`wezterm-gui/src/frontend.rs`).

The three compounding bugs (and their fixes):

| | Bug | Where | Fix in PR #7763 |
|---|---|---|---|
| **P1** | A `PaneFocused` for a destroyed pane still spawns a doomed reconcile task, giving the `pane N not found` flood | `frontend.rs` | guard `Mux::get().get_pane(id).is_some()` before spawning |
| **P2** | Every focus change emits `PaneFocused` **twice** — once in `advise_focus_change`, once at the call site | `mux/src/tab.rs`, `sessionhandler.rs`, `tmux_commands.rs`, `mux/src/lib.rs` | `NotifyMux::No` suppresses the `advise_focus_change` copy |
| **P3** | Receiving a server `PaneFocused` makes the client `focus_changed`, which echoes `SetFocusedPane` back | `clientpane.rs` | pre-set `focused_remote_pane_id` so `advise_focus` sees it's already focused and skips the echo |

P1 produces the *log flood / wasted UI-thread tasks*. **P2 + P3 are what make the loop
self-sustaining** — the doubling feeds the queue and the echo regenerates the input.

---

## BEFORE — steady-state loop (a single focus change never settles)

`P2` (server doubles every notify) plus `P3` (client echoes every notify back) form a
closed feedback loop with 2x amplification per round. One user action seeds it, then it
runs on its own with no further input until a core is pinned.

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#ffffff','primaryColor':'#f6f8fa','primaryTextColor':'#24292f','primaryBorderColor':'#24292f','lineColor':'#24292f','actorBkg':'#f6f8fa','actorBorder':'#24292f','actorTextColor':'#24292f','signalColor':'#24292f','signalTextColor':'#24292f','noteBkgColor':'#fff8c5','noteTextColor':'#24292f','noteBorderColor':'#d4a72c','loopTextColor':'#24292f','labelBoxBkgColor':'#f6f8fa','labelTextColor':'#24292f','sequenceNumberColor':'#ffffff'}}}%%
sequenceDiagram
    autonumber
    actor U as User
    participant G as GUI
    participant S as MUX
    Note over G: GUI is wezterm-gui, the client on the Mac
    Note over S: MUX is wezterm-mux-server on Linux

    U->>G: switch focus to pane B
    G->>G: ClientPane B focus_changed true
    G->>S: SetFocusedPane B
    Note over S: SessionHandler handles SetFocusedPane
    S->>S: tab set_active_pane B
    S->>S: advise_focus_change fires PaneFocused B
    Note over S: P2 call site ALSO fires PaneFocused B
    S-->>G: PaneFocused B
    S-->>G: PaneFocused B duplicate
    Note over G: P1 every notification spawns a reconcile task, no guard
    G->>G: focus_pane_and_containing_tab B
    G->>G: ClientPane B focus_changed true
    Note over G: P3 focused_remote_pane_id not preset, so it echoes
    G->>S: SetFocusedPane B echo
    loop never terminates, doubles each round
        S-->>G: PaneFocused B twice
        G->>S: SetFocusedPane B
    end
    Note over G,S: queue never drains, one core pinned near 100 percent
```

## BEFORE — domain-detach storm (the `pane N not found` flood)

When the domain detaches, the client tears down its local panes, but `PaneFocused`
notifications for those now-destroyed ids are already queued. With no existence check
(P1), each spawns a doomed reconcile task.

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#ffffff','primaryColor':'#f6f8fa','primaryTextColor':'#24292f','primaryBorderColor':'#24292f','lineColor':'#24292f','actorBkg':'#f6f8fa','actorBorder':'#24292f','actorTextColor':'#24292f','signalColor':'#24292f','signalTextColor':'#24292f','noteBkgColor':'#fff8c5','noteTextColor':'#24292f','noteBorderColor':'#d4a72c','loopTextColor':'#24292f','labelBoxBkgColor':'#f6f8fa','labelTextColor':'#24292f','sequenceNumberColor':'#ffffff'}}}%%
sequenceDiagram
    autonumber
    participant S as MUX
    participant G as GUI

    Note over S,G: SSHMUX domain detaches, link drop or window close
    S-->>G: in-flight PaneFocused 46
    S-->>G: in-flight PaneFocused 47
    G->>G: tear down local panes 38 to 52, so 46 and 47 are gone
    Note over G: but PaneFocused 46 and 47 are still queued
    loop drains forever, P1 no existence check
        G->>G: reconcile focus_pane_and_containing_tab 46
        G-->>G: ERROR pane 46 not found
        G->>G: reconcile focus_pane_and_containing_tab 47
        G-->>G: ERROR pane 47 not found
    end
    Note over G: log floods, main thread pinned, survives mux disconnect
```

---

## AFTER — clean focus change (loop broken at both ends)

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#ffffff','primaryColor':'#f6f8fa','primaryTextColor':'#24292f','primaryBorderColor':'#24292f','lineColor':'#24292f','actorBkg':'#f6f8fa','actorBorder':'#24292f','actorTextColor':'#24292f','signalColor':'#24292f','signalTextColor':'#24292f','noteBkgColor':'#fff8c5','noteTextColor':'#24292f','noteBorderColor':'#d4a72c','loopTextColor':'#24292f','labelBoxBkgColor':'#f6f8fa','labelTextColor':'#24292f','sequenceNumberColor':'#ffffff'}}}%%
sequenceDiagram
    autonumber
    actor U as User
    participant G as GUI
    participant S as MUX

    U->>G: switch focus to pane B
    G->>G: ClientPane B focus_changed true
    G->>S: SetFocusedPane B
    rect rgb(230,255,236)
    Note over S: P2 fix
    S->>S: set_active_pane_with_notify B NotifyMux No
    Note over S: advise_focus_change is suppressed, no notify
    S->>S: call site fires PaneFocused B exactly once
    S-->>G: PaneFocused B single
    end
    rect rgb(230,255,236)
    Note over G: P1 fix get_pane B is_some true, so spawn one task
    Note over G: P3 fix preset focused_remote_pane_id to B
    G->>G: focus_pane_and_containing_tab B then focus_changed true
    G->>G: advise_focus sees already focused, sends nothing
    end
    Note over G,S: no duplicate, no echo, queue drains, both idle
```

## AFTER — domain detach (stale notifications dropped)

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#ffffff','primaryColor':'#f6f8fa','primaryTextColor':'#24292f','primaryBorderColor':'#24292f','lineColor':'#24292f','actorBkg':'#f6f8fa','actorBorder':'#24292f','actorTextColor':'#24292f','signalColor':'#24292f','signalTextColor':'#24292f','noteBkgColor':'#fff8c5','noteTextColor':'#24292f','noteBorderColor':'#d4a72c','loopTextColor':'#24292f','labelBoxBkgColor':'#f6f8fa','labelTextColor':'#24292f','sequenceNumberColor':'#ffffff'}}}%%
sequenceDiagram
    autonumber
    participant S as MUX
    participant G as GUI

    Note over S,G: SSHMUX domain detaches
    S-->>G: in-flight PaneFocused 46
    S-->>G: in-flight PaneFocused 47
    G->>G: tear down panes, 46 and 47 destroyed
    rect rgb(230,255,236)
    Note over G: P1 fix existence check before spawning
    G->>G: get_pane 46 is_some false, so drop, no task
    G->>G: get_pane 47 is_some false, so drop, no task
    end
    Note over G: nothing spawned, no flood, no error storm
```

---

## Why each fix alone is insufficient

- **P1 only** (guard) stops the `pane N not found` flood and saves UI-thread tasks, but the
  doubling plus echo still loop over *live* panes, so the focus still ping-pongs.
- **P2 only** (no double) halves the traffic, but the echo still sustains a 1x loop.
- **P3 only** (no echo) stops the client re-injecting, but a detach burst of stale
  notifications still floods the UI thread.

All three together: one focus change produces exactly one notification, reconciled once,
with no echo and no stale-pane work — the loop cannot form.
