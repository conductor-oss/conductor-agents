# Platform packs

The swarm is generic: the lead picks the stack and agents use the host's tools. What each kind of system needs
from the harness is declared once per platform, in `workers/common/stacks.py`:

| Field | What the harness does with it |
|---|---|
| `markers` | Detects the pack from files in the tree (`go.mod`, `Package.swift`, `pubspec.yaml`, ...). |
| `tools` | Checks the host right after planning (`swarm_host_check`). A missing toolchain (no Xcode for an iOS app) becomes a request to an approver: someone installs it and grants, the harness checks again. Denied, the run builds without it and the board says so. |
| `caches` | Points the pack's caches inside the tree for every session and check run (the sandbox writes only there). |
| `hosts` | Adds the pack's package hosts to the sandbox's allowed hosts for this run only. |
| `tests` | Gives the pack's test runners the expected-duration wrappers (`common/test_budget.py`). |
| `ui` | How an acceptance check drives the app's interface. |
| `smoke`, `split` | Guidance the lead reads when it splits and plans: the default smoke check, and how to split so parallel agents never edit one file (an Xcode project is split into SwiftPM or XcodeGen modules, never one shared `project.pbxproj`). |

Packs: `web`, `node`, `jvm`, `go`, `python`, `rust`, `dotnet`, `cpp`, `ruby`, `php`, `elixir`, `ios`, `android`,
`flutter`, `react-native`, `electron`, `tauri`, `iac`, `data-ml`.

The lead declares `stacks` in the kickoff (team runs) and the plan; the harness adds what the tree shows and
records the result in `.cc/stacks.json`.

## App UI checks on a device

The sandbox cannot boot an iOS simulator, start an Android emulator or open an Electron window, so these run as a
host service, like the browser (`common/device_service.py`). A session or check calls:

```
.cc-cache/bin/device ios --scheme App [--project App.xcodeproj | --workspace App.xcworkspace] [--only-testing T]
.cc-cache/bin/device android [--module app] [--avd NAME]
.cc-cache/bin/device electron .cc/checks/<name>.mjs [--main main.js]
```

Each job gets a device of its own (a fresh simulator, a read-only emulator on a free port, a new Electron
process), runs one fixed command built by the service (never a shell line), and removes the device afterwards.
The service accepts jobs only with this host's token and only for a harness tree, and paths must stay inside
that tree. It starts only for runs whose packs need it.

Proven on this host: detection, tool checks, caches, hosts, the service's token and tree checks, and the
Electron and iOS jobs' messages when the tool is missing. Not yet run end to end: the iOS driver (needs Xcode
and a simulator runtime), the Android driver (needs the Android SDK, emulator and a virtual device) and the
Electron driver against a real app (needs `npm install electron` in the tree).
