You write the CONFORMANCE ADAPTER for a swarm about to build this request, in this repository (branch `{{branch}}`; do not commit: the harness commits the tree):

{{request}}

The architect wrote the TLA+ spec in `.cc/spec/` (the `.tla` module, its `.cfg`, and `abstraction.json` with its `mapping`: how each spec action is performed through the program's public interface and how each state variable is read back). The harness replays every transition of the spec's bounded model through your adapter against the real program after every round; a difference between the program and the spec is reported as the shortest sequence of actions that shows it. The adapter is the abstraction function: it must map faithfully, never fix up or hide what the program does.

Write `.cc/conformance/adapter.mjs` (Node, no dependencies beyond Node's standard library):
- It reads JSON lines on stdin and answers exactly one JSON line on stdout for each, and prints nothing else on stdout (logs go to stderr):
  `{"op":"reset"}` -> `{"ok":true}` — empty all of the program's data.
  `{"op":"step","action":"<Action>","args":[...]}` -> `{"ok":true,"result":...}` — perform the action with the spec's positional arguments through the program's interface, and map the program's answer to the value the spec's result variable would hold.
  `{"op":"state"}` -> `{"ok":true,"state":{...}}` — read the program's state back in exactly the spec's shape for every variable listed in `abstraction.json` `"state"`: a TLA+ function becomes an object keyed by its domain, a set a list, a record an object, a sequence a list.
- It starts the program itself the way the design says (choosing its own free port), waits for it with a bound, and stops it when stdin closes; it never leaves anything running.
- Arguments and values are the spec's constants (strings, numbers); keep the mapping exactly as `abstraction.json` describes.

The program does not exist yet, so the adapter cannot pass now: check that it parses (`node --check .cc/conformance/adapter.mjs`) and that it answers `{"op":"reset"}` with a clear error rather than hanging when the program cannot start. Report the files you wrote.
