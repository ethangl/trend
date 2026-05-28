# Trend menubar app

A SwiftUI menubar app that reads `~/.trend/status.json` (written by the Python
loop in `../scripts/run_live_loop.py`) and shows positions, reconcile state,
and recent fills. Also supervises the Python loop as a child process —
Start / Stop / Restart from the menu, auto-restarts on crash.

## Layout

```
macos-app/
├── README.md                          (this file)
├── TrendMenu.xcodeproj/               (open this in Xcode)
└── TrendMenu/
    ├── TrendMenuApp.swift             @main, AppDelegate, MenuBarExtra
    ├── Status.swift                   Codable model + StatusReader (polls JSON)
    ├── MenuView.swift                 the dropdown
    ├── ProcessSupervisor.swift        spawns/supervises the Python loop
    ├── ContentView.swift              (template leftover, unused — safe to remove in Xcode)
    └── Assets.xcassets/
```

## Open & build

```
open ~/w/trend/macos-app/TrendMenu.xcodeproj
```
Then ⌘R. The menubar will show a dot + "trend".

## Disable "Approachable Concurrency" (Xcode 26+)

The Mac App template in recent Xcode enables `SWIFT_APPROACHABLE_CONCURRENCY = YES` and `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`. That implicitly puts every type on the main actor, which fights `ObservableObject`'s nonisolated `objectWillChange` requirement.

In the project settings → **Build Settings** tab → search for "Approachable" and "Default Actor Isolation":

| Setting                       | Set to        |
|-------------------------------|---------------|
| Approachable Concurrency      | No            |
| Default Actor Isolation       | nonisolated   |

Without this, you'll get `Type 'StatusReader' does not conform to protocol 'ObservableObject'`.

## Disable App Sandbox

The Mac App template also enables App Sandbox by default, which blocks reading anywhere outside the app's container — so `~/.trend/status.json` is invisible. In the same **Build Settings** pane:

| Setting              | Set to |
|----------------------|--------|
| App Sandbox          | No     |

(For a personal trading tool on your own Mac, sandboxing buys nothing. If you ever want to distribute the app, switch sandbox back on and grant access to `~/.trend/` via a `com.apple.security.files.user-selected.read-only` entitlement + `NSOpenPanel` bookmark, or relocate the status file into the app's container.)

## Settings already applied in this project

The committed `TrendMenu.xcodeproj` has the three template defaults already disabled (see `project.pbxproj`): `SWIFT_APPROACHABLE_CONCURRENCY = NO`, `SWIFT_DEFAULT_ACTOR_ISOLATION = nonisolated`, `ENABLE_APP_SANDBOX = NO`. The build sections above describe what they do and how to find them if you recreate the project from scratch.

## Make it menubar-only (no Dock icon)

In Xcode, click the project at the top of the navigator, select the `TrendMenu` **target**, then the **Info** tab. Add a row:

| Key                                  | Type    | Value |
|--------------------------------------|---------|-------|
| `Application is agent (UIElement)`   | Boolean | `YES` |

(Internally this sets `LSUIElement = true`. Without it the app appears in the Dock and Cmd-Tab switcher, which we don't want.)

## Build & run

1. Press **⌘R** (or the Play button). The app launches with a status dot + "trend" in the menubar.
2. Click it — you'll see "Unknown" with a gray dot until the Python loop is running and has written `~/.trend/status.json`.
3. Start the loop:
   ```
   .venv/bin/python ~/w/trend/scripts/run_live_loop.py --skip-symbols MBT,MET --once
   ```
   The `--once` path now also emits `status.json`. Click the menubar icon — the view should populate.

## What you should see

- Green dot when `status=waiting/idle`, `ib_connected=true`, no halted reconcile.
- Yellow during `starting`/`ticking` or if IB is disconnected.
- Red if a `last_tick.severity=halt` happened or `recent_errors` is non-empty.
- Positions section shows `expected / actual`; the actual cell turns red on mismatch.

## What's next

- **Phase 2**: have the Swift app launch the Python loop as a child process and restart it on crash (replaces our launchd plan).
- **Phase 3**: control buttons (Flatten, Pause/Resume, Skip Symbol, Restart) wired via a command file the Python loop polls.

## Notes for the SwiftUI learner

- `@StateObject` (in the App) owns the reader for the app's lifetime. `@ObservedObject` (in MenuView) just observes it.
- `@Published` properties on the reader trigger SwiftUI re-renders automatically when they change.
- `@MainActor` on `StatusReader` is Swift Concurrency saying "all access happens on the main thread" — required because SwiftUI views must update on main.
- `Codable` + `CodingKeys` is how you map JSON snake_case to Swift camelCase. The `JSONDecoder` walks the struct's `CodingKeys` and looks for those JSON keys.
- `Timer.scheduledTimer` is the simplest poll mechanism. Later you can switch to `DispatchSource.makeFileSystemObjectSource` to react instantly to file writes — but 2s polling uses essentially zero CPU.
