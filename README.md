# Roblox Auto-Rejoin for UGPhone (Termux)

Watches Roblox from Termux and relaunches your chosen experience when Roblox closes. Python standard library only, no root required.

## Status of verification (read first)

| Part | Verified how |
|---|---|
| URL/ID parsing, config, state machine, retry/backoff, duplicate-launch guard, probe logic, disconnect/pid-change logic | Unit tests (50, simulated shell). Run on Windows. |
| Real `am`/`pm`/`pidof`/`dumpsys` behaviour on UGPhone | **Not verified.** Built without access to a UGPhone. The tool measures it on the device via `diagnose`. |
| Entering the game server | **Never verifiable.** Only "Roblox process is up" can be detected. |

No UGPhone-specific API is used. UGPhone is treated as ordinary Android.

## Install (Termux)

```sh
pkg update -y && pkg install -y python termux-api git
mkdir -p ~/roblox-auto-rejoin && cd ~/roblox-auto-rejoin
# copy the project files here (e.g. via UGPhone file upload, or git clone your repo)
python main.py diagnose
```

`termux-api` also needs the **Termux:API** app installed for `termux-wake-lock` / `termux-open-url`. Both are optional.

## Run

```sh
python main.py            # interactive menu
python main.py set-game https://www.roblox.com/games/1234567890/Example-Game
python main.py diagnose   # capability report
python main.py diagnose --launch-test   # also starts Roblox once to prove launching works
python main.py run        # unattended monitoring with saved settings
```

Keep it alive and restart after a Termux interruption:

```sh
pkg install -y tmux
tmux new -d -s rejoin 'cd ~/roblox-auto-rejoin && while true; do python main.py run; sleep 10; done'
tmux attach -t rejoin    # view; detach with Ctrl+b then d
```

After Termux is killed or the cloud phone resets, reopen Termux and run the `tmux new ...` line again. Survival of a full cloud-phone reset is **not** claimed; nothing here auto-starts at boot.
The lock file `rejoin.lock` stops two monitors running at once; stale locks are reclaimed.

## How detection works (and its limits)

Android 7+ usually hides other apps' processes from Termux. So `pidof com.roblox.client` returning "nothing" would look the same as a crash. The tool therefore **calibrates** each probe first:

- `pidof` / `ps`: trusted to say "not running" only if they can see system processes (`system_server`, `zygote`...).
- `dumpsys`: trusted only if readable. It needs the DUMP permission (adb shell or root), and also provides foreground vs background.
- A positive sighting is always believed. A probe that cannot be trusted yields `UNKNOWN`, never `STOPPED`.

States: `FOREGROUND`, `BACKGROUND` (running, user switched apps: ignored), `RUNNING` (up, foreground unknown), `STOPPED`, `UNKNOWN`. A loading screen cannot be distinguished from a running game; it is not attempted.

A close is acted on only after `confirm_polls` consecutive `STOPPED` readings. `UNKNOWN` never triggers a launch.

### Roblox alive but out of the game

A crash does not always kill the process: Android may restart Roblox by itself, or Roblox may disconnect you and stay on its home screen. Both look like "running" to a process check, so two more signals run while Roblox is up (each is a setting, default on):

- `rejoin_on_pid_change`: the Roblox pid changed, meaning the process restarted. Needs `pidof` to see Roblox (root/adb prefix).
- `rejoin_on_disconnect`: Roblox logged `Sending disconnect with reason: <200-599>` in logcat (`logcat --pid`). Only lines written after monitoring started count. **Unverified on UGPhone**: check the log line exists on your device (`diagnose` reports whether the logcat is readable).

When either fires, `am force-stop` runs first (`force_stop_before_launch`, needs root/adb; if denied it is logged and skipped), then the game is relaunched. Success requires a *new* pid.
Cost: one `pidof` and one `logcat -d -t 800` per poll while Roblox runs.

If `diagnose` says crash detection is **not available**, choose a fallback:

1. **ADB loopback (best, unverified on UGPhone):** if UGPhone lets you enable ADB / developer options, connect from Termux (`pkg install android-tools; adb connect localhost:5555`) and set `"shell_prefix": "adb -s localhost:5555 shell"`. Every command then runs with shell privileges and `dumpsys` works. Re-run `diagnose` to confirm.
2. **Root (`"shell_prefix": "su -c"`):** only if your UGPhone image already provides `su`. Never required.
3. **Blind mode:** `"blind_relaunch_minutes": N` relaunches the experience every N minutes regardless of state. Crude: it can disturb a running game. Off by default.

## Launching

`am start -a android.intent.action.VIEW -d roblox://experiences/start?placeId=<ID> -p <package>`, then `roblox://placeId=<ID>`, then `termux-open-url`. "Launch sent" means Android accepted the intent. It is reported as success only after the monitor sees Roblox running; otherwise it logs "cannot confirm". Reopening a link does not restore the previous server.
The ID is the number in `roblox.com/games/<ID>/...` (a place ID).
On Android 10+ a background app starting activities can be blocked: grant Termux **Display over other apps**.

## Settings that matter on the device

Inside UGPhone / Android (verify each in your own instance; names vary by version):
- Termux: Battery -> **Unrestricted / Don't optimize**.
- Termux: **Display over other apps** allowed.
- Run `termux-wake-lock` (done automatically if Termux:API is present). The notification "Acquire wakelock" in Termux does the same.
- Keep the UGPhone session/instance running; if the platform stops the instance, Termux stops too.

`config.json` (auto-created, no credentials ever stored): `experience_id`, `saved_experiences`, `poll_interval`, `confirm_polls`, `reopen_delay`, `max_retries` (0 = unlimited), `retry_delay`, `backoff_factor`, `retry_delay_max`, `min_launch_interval`, `verify_timeout`, `give_up_cooldown`, `launch_on_start`, `recover_if_closed_on_start`, `blind_relaunch_minutes`, `log_level`, `roblox_package`, `shell_prefix`. Invalid values are rejected with a reason.

Network loss: recovery pauses until `www.roblox.com:443` is reachable; no retry attempts are used meanwhile. Logs: `rejoin.log` (rotating).

## Device-side validation (do this on UGPhone)

1. `python main.py diagnose` - note which probes are OK. Needs `am`, `pm`, Roblox package OK.
2. `python main.py diagnose --launch-test` - Roblox must open and be detected.
3. `python main.py set-game <your URL>` then `python main.py run`.
4. Swipe Roblox to the background: status must show `BACKGROUND`/`RUNNING`, **no** relaunch.
5. Force-stop Roblox (Settings -> Apps -> Roblox -> Force stop). Expect: 3 stopped polls, wait `reopen_delay`, launch, "Roblox launch detected".
6. Turn network off for ~30 s while Roblox is closed: expect "Network unreachable", then launch after it returns.
7. Close Termux, reopen, rerun the tmux line: monitoring resumes.

## Tests

```sh
python -m unittest discover -s tests -t .
```
All simulated (fake shell/clock). There are no automated real-Android integration tests; use the checklist above.

## Known limitations

- Crash detection depends on what UGPhone exposes to Termux; may be unavailable without ADB/root.
- Cannot confirm the game server was joined, or distinguish loading screen from gameplay.
- Roblox may show its own dialogs (update, ban, "disconnected") that need manual action; a running process is considered healthy.
- Roblox package auto-detection may fail under Android 11+ package visibility; set `roblox_package`.
