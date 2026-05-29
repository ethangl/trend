# IB Gateway auto-login (IBC + launchd)

Keeps IB Gateway logged in and supervised so the live loop can connect on
port 4002 unattended.

Chain: `launchd` → `gatewaystart-keychain.sh` → IBC → IB Gateway → Python loop.

- **launchd** (`~/Library/LaunchAgents/com.trend.ibgateway.plist`) — `RunAtLoad`
  + `KeepAlive`; relaunches IBC if it dies. Runs in the GUI session (Gateway is
  a Swing app and needs a logged-in desktop).
- **`~/ibc/gatewaystart-keychain.sh`** — thin wrapper: sets a writable `TMPDIR`,
  `cd "$HOME"`, then `exec`s `~/ibc/gatewaystartmacos.sh -inline`. It does **not**
  set `TWSUSERID`/`TWSPASSWORD`, so IBC takes credentials from `config.ini`
  instead of the command line (the filename keeps the `-keychain` suffix only so
  the launchd plist works without a reload; keychain is no longer used).
- **One patch to IBC's shipped scripts** (re-apply after any IBC upgrade — an
  upgrade overwrites it): the wrapper does `cd "$HOME"` + sets `TMPDIR` because
  launchd starts it with `CWD=/` (read-only), and `ibcstart.sh`'s
  `mktemp -u XXXXXXXX` generates its session-id name relative to CWD (otherwise:
  `mktemp: ... Read-only file system` and an empty `ibcsessionid`).
- **IBC 3.23.0** (`~/ibc`, config `~/ibc/config.ini`) — types the creds into the
  Gateway login form. **`config.ini` holds `IbLoginId`/`IbPassword`** and is mode
  **600 (owner-only)**. `TradingMode=paper`, API port `4002`,
  `AcceptIncomingConnectionAction=accept`, `AutoRestartTime=03:00 AM`.
- **Auto-restart** — daily 03:00 CT (= 04:00 ET, Gateway's `jts.ini` timezone is
  `America/Chicago`). Restarts in-process reusing the session token: no password,
  no 2FA, fully unattended.

## Credentials

Username and password live in `~/ibc/config.ini` as `IbLoginId` / `IbPassword`.
The file is `chmod 600` (owner-only) — keep it that way:

```bash
chmod 600 ~/ibc/config.ini
ls -l ~/ibc/config.ini   # expect -rw-------
```

Because credentials are read from `config.ini`, they are **not** passed on the
java command line and never appear in `ps` / Activity Monitor / process args.
IBC only reads `IbLoginId`/`IbPassword` from the file when nothing is supplied on
the command line — so the wrapper must not export `TWSUSERID`/`TWSPASSWORD`.

To change the password: edit the `IbPassword=` line in `config.ini` and restart
the Gateway (see below). Keep `~/ibc` off any synced/backed-up volume (iCloud,
Dropbox, etc.) and out of version control — plaintext-at-rest is only safe while
"rest" stays on this disk.

## Manage the LaunchAgent

```bash
# start / reload (reload = bootout then bootstrap; restarts Gateway)
launchctl bootout    gui/$(id -u)/com.trend.ibgateway 2>/dev/null
launchctl bootstrap  gui/$(id -u) ~/Library/LaunchAgents/com.trend.ibgateway.plist

# force a restart in place (re-reads config.ini)
launchctl kickstart -k gui/$(id -u)/com.trend.ibgateway

# status
launchctl print gui/$(id -u)/com.trend.ibgateway | grep -E 'state|pid'

# stop (and keep it stopped)
launchctl bootout gui/$(id -u)/com.trend.ibgateway
```

Logs: `~/ibc/logs/launchd.log` (wrapper output) and
`~/ibc/logs/ibc-*_GATEWAY-*.txt` (IBC diagnostics, login state, auto-restart).

Note: restarting the Gateway makes the live loop reconnect and run a cold
catch-up (re-fetching recent daily bars), which can take several minutes if the
HMDS data farm is slow — time restarts accordingly.

## Paper vs. live

- **Paper** — no 2FA, so auto-login is 100% unattended, including the weekly
  forced re-auth.
- **Live** — username/password still auto-fill from `config.ini` (set
  `TradingMode=live`), but IBKR requires an **IBKR Mobile 2FA tap** on each
  fresh/weekly login that cannot be scripted away. The daily 04:00 token-reuse
  restart stays hands-off. For live, weigh whether owner-only plaintext on disk
  is acceptable for a real-money login, or use a more protected secret store.
