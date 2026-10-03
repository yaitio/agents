# Sandbox tools — requirements

Status: **specification**, not built. What the six file-and-shell tools must do
when this deployment gets a sandbox, so that an agent written for Anthropic's
`agent_toolset_20260401` runs here unchanged.

## Shape

- **One container per session**, started on the first call to any of these
  tools, not when the session is made: most sessions never need one.
- **The tools are an MCP server inside the container.** The step worker calls
  them like any other MCP tool (`src/tools.py`), so the loop, the events, the
  truncation and the timeouts already in place apply. The container is not
  reachable from the internet; the worker reaches it over the session's private
  connection.
- **Names and inputs are Anthropic's**: `bash`, `read`, `write`, `edit`, `glob`,
  `grep`, with the fields below and no others. An unknown field is an error, not
  ignored.
- **The working directory is `/workspace`**, owned by the sandbox user. A
  relative path is relative to it. Paths outside `/workspace` and `/tmp` are
  refused, `..` included, after resolving symlinks.
- **It lives as long as the session**, and stops after `idle_timeout` (default
  15 min) without a call, when the session is archived or deleted, or at
  `max_lifetime` (default 2 h). Files may be snapshotted to the session's
  storage on stop and restored on the next start; the shell's state is not.

## Common rules

| Rule | Value |
|---|---|
| Result | Text. Errors are results too: `is_error: true` and a message the model can act on, never an exception that fails the turn |
| Result size | At most 64 KB (the worker's `YAIT_TOOL_RESULT_CHARS`). A longer one is cut, saying how long it was and how to read the rest |
| Encoding | UTF-8. A file that is not valid UTF-8 is binary: `read` says so and gives its size and type rather than its bytes |
| Line numbers | 1-based, inclusive |
| Time | Every call has a timeout; a call that runs out says so, and what it had produced until then |
| Concurrency | Calls in one session run one at a time, in the order the model made them |

## `bash`

Runs a command in **one persistent shell** per session: the working directory,
environment variables, shell functions and background processes survive between
calls.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `command` | string | unless `restart` | The command, run by `bash -lc`-like semantics in the persistent shell |
| `timeout_ms` | integer | no | Per-call limit. Default 120 000, at most 600 000 |
| `restart` | boolean | no | `true` kills the shell and starts a fresh one in `/workspace`; `command` is then not allowed |

Result: stdout and stderr interleaved as they were written, then the exit code
on a line of its own.

```json
{"command": "cd app && python -m pytest -q"}
```
```
..F.
1 failed, 3 passed in 0.42s
[exit code 1]
```

- A non-zero exit code is a normal result (`is_error: false`): a failing test is
  information, not a tool failure. `is_error: true` is for the tool itself —
  a timeout, a refused command, a shell that died.
- On timeout the command is killed (the process group), the output so far is
  returned with `[timed out after 120000 ms]`, and the shell stays usable.
- Interactive programs get no terminal; a command waiting for input is ended by
  the timeout. `sudo` is unavailable.
- `cd` persists: a later `{"command": "pwd"}` prints `/workspace/app`.
- `restart` answers `shell restarted in /workspace`.

## `read`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `file_path` | string | yes | The file |
| `view_range` | `[start, end]` integers | no | Lines to show, inclusive. `end` of `-1` means to the end |

Result: the lines, each prefixed with its number and a tab, so `edit` and the
model can point at them.

```json
{"file_path": "app/main.py", "view_range": [1, 3]}
```
```
1	import sys
2
3	def main():
```

- Without `view_range`, at most the first 2 000 lines; a longer file ends with
  `[file has 5 812 lines; pass view_range to read more]`.
- A directory gives its entries, one per line, directories marked with `/`.
- A missing file: `is_error: true`, `no such file: app/mian.py`, and the nearest
  existing names in that directory.

## `write`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `file_path` | string | yes | The file; missing parent directories are created |
| `content` | string | yes | The whole new contents |

Result: `wrote app/notes.md (3 lines, 82 bytes)`, or `created` for a new file.

- Overwrites without asking: it is the call for a whole file. For a change to
  part of one, `edit`.
- At most 1 MB of content per call.

```json
{"file_path": "notes.md", "content": "# Notes\n\n- first\n"}
```

## `edit`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `file_path` | string | yes | An existing file |
| `old_string` | string | yes | Exact text to find, whitespace included |
| `new_string` | string | yes | Its replacement; must differ from `old_string` |
| `replace_all` | boolean | no | Replace every occurrence. Default `false` |

Result: `replaced 1 occurrence in app/main.py`, and the changed lines with a
line of context on each side, numbered as `read` numbers them.

- `old_string` found **0** times: `is_error: true`, and the closest match with
  its line number, so the model can correct the whitespace.
- Found **more than once** without `replace_all`: `is_error: true`,
  `old_string occurs 3 times (lines 12, 40, 77); add context or set replace_all`.
  Nothing is changed.
- The file is written whole and atomically; a failed edit leaves it untouched.

```json
{"file_path": "app/main.py", "old_string": "return 1", "new_string": "return 0"}
```

## `glob`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `pattern` | string | yes | A doublestar pattern: `**/*.py`, `src/*.{ts,tsx}` |
| `path` | string | no | The directory to search under. Default `/workspace` |

Result: matching paths, relative to `path`, newest first, one per line; at
most 1 000, then `[1 000 of 4 210 matches shown]`. No match is an empty result
with `no files match **/*.rs`, not an error.

- Hidden files and `.git/` are skipped unless the pattern names them.

```json
{"pattern": "**/test_*.py"}
```
```
tests/test_api.py
tests/test_store.py
```

## `grep`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `pattern` | string | yes | A regular expression (ripgrep syntax) |
| `path` | string | no | A file or directory. Default `/workspace` |

Result: `path:line:text` per match, grouped by file; at most 500 matches, then
how many more there were. Binary files and `.git/` are skipped. An invalid
regular expression is `is_error: true` with the parser's message.

```json
{"pattern": "def (main|run)\\(", "path": "app"}
```
```
app/main.py:3:def main():
app/cli.py:18:def run(argv):
```

## Agent configuration

As in `agent_toolset_20260401`: the set is enabled by
`{"type": "agent_toolset_20260401"}` in the agent's `tools`, and each tool can be
turned off or set to ask first:

```json
{"type": "agent_toolset_20260401",
 "default_config": {"enabled": true, "permission_policy": {"type": "always_allow"}},
 "configs": [{"name": "bash", "permission_policy": {"type": "always_ask"}},
             {"name": "write", "enabled": false}]}
```

`always_ask` needs approvals (`user.tool_confirmation`), which are not served
yet; until they are, it is refused when the agent is made, as for MCP tools.

## Events

The same as the original's for its own tools: `agent.tool_use` (name, input) and
`agent.tool_result` (`tool_use_id`, content, `is_error`) in the Anthropic dialect;
in the OpenAI dialect, the item types its document defines for command
execution, or a function-call item where it has none.

## Environment and limits

| | Default | Set by |
|---|---|---|
| Image | Ubuntu LTS with Python, Node, git, ripgrep | the environment resource |
| Packages | `pip`, `npm`, `apt` lists, installed at start | the environment resource |
| CPU / memory / disk | 1 vCPU, 2 GB, 10 GB | the installation |
| Network | none, or a list of allowed hosts (and package registries) | the environment resource |
| Credentials | none in the container: no AWS role, no provider key, no vault secret | — |

## Acceptance

Each tool by example above, plus:

1. State survives: `cd`, an exported variable and a background server started in
   one `bash` call are there in the next.
2. A command that sleeps past `timeout_ms` is killed, its output so far returned,
   and the next call works.
3. `edit` with an ambiguous `old_string` changes nothing and names the lines.
4. A path outside `/workspace` and `/tmp`, by `..` or by symlink, is refused.
5. A 10 MB `bash` output reaches the model cut to 64 KB, saying so.
6. With network `none`, `curl https://example.com` fails; with the host allowed,
   it succeeds.
7. Two sessions never see each other's files or processes.
8. Nothing of the caller — provider key, vault secret — is readable inside the
   container (`env`, `/proc`, the metadata endpoint).
9. The same agent and task on Managed Agents and here give the same sequence of
   tool events.
