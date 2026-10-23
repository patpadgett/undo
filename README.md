# undo — journal-of-inverses shell undo (v0.2.1)

[![CI](https://github.com/patpadgett/undo/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/patpadgett/undo/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.2.1-blue.svg)](https://github.com/patpadgett/undo/releases)

A small, standard-library-only Python 3.9+ engine and bash/zsh shell functions.
It remembers how to reverse `mv`, `cp`, `rm`, `chmod`, `chown`, `mkdir`,
`rmdir`, and `ln`. **This is best-effort recovery, not a backup or a
snapshotting filesystem. Read the limitations before installing.**

## Install

Clone the repository somewhere permanent and add this line to `~/.bashrc` or `~/.zshrc`:

```sh
source ~/src/undo/undo.sh      # wherever you cloned it
```

Or set `UNDO_PY=/path/to/undo.py` before sourcing another copy of the script.
Without the override, the engine is located relative to the sourced script
using `${BASH_SOURCE[0]:-${(%):-%x}}`. Requires `python3` on PATH. No packages,
daemon, elevated privileges, or network connection are required.

## Usage

```sh
mv 'draft notes' 'final notes'
undo -n                  # inspect without changing anything
undo                     # show original command/inverses; undo? [y/N]
undo -y                  # reverse the last successful operation
undo -l                  # last 10 entries, including no-op/failed/undone entries
undo -l 25
undo --list 25
undo ENTRY_ID            # choose an exact id
undo -y ENTRY_ID
undo --recover ENTRY_ID  # retry a failed, pending, or interrupted entry
undo --recover -y        # retry the newest recoverable entry
undo --doctor            # check state permissions, journal health, trash
undo --repair            # preserve a corrupt journal and salvage valid entries
undo --purge             # permanently expire old entries and trash
undo --force ENTRY_ID    # allow out-of-order undo or changed paths
undo --version
UNDO_DISABLE=1 mv a b    # bypass this invocation
command rm file          # bypass the shell function
```

`undo --force` is deliberately blunt: it bypasses both the out-of-order warning
(for an exact older id) and the copy-conflict check. Prefer undoing newer
entries first.

**Bash/zsh detail:** `\rm` bypasses an *alias*, not a shell function; it does
NOT bypass these wrappers. Use `command rm`, `/bin/rm`, or `UNDO_DISABLE=1`.
Existing aliases can prevent these functions from being selected: remove
conflicting aliases before sourcing. The wrapper blocks obvious `.`/`..`, root,
`--no-preserve-root`, and the current/state directory before any fallback, but
it is not a complete GNU rm emulator. These wrappers are intended for interactive
shells, not production scripts relying on exact GNU rm behavior.

## Supported options

| Command | Journaled options |
|---|---|
| `mv` | `-f -i -v -n -T -t DIR` and common long equivalents |
| `cp` | `-f -i -v -n -T -t DIR -r -R -a -d -L -H -l -s -p -P` |
| `rm` | `-f -r -R -v` and common long equivalents |
| `chmod` | `-R -v -c -f` and `--recursive` |
| `chown` | `-R -v -c -f -h --no-dereference` |
| `mkdir` | `-p -v` |
| `rmdir` | `-v` |
| `ln` | `-s -i -v` |

Common long forms (`--archive`, `--recursive`, `--force`, `--verbose`,
`--interactive`, `--no-clobber`, `--target-directory`, `--link`,
`--symbolic-link`, `--no-dereference`, `--parents` for `mkdir`) are recognized
where they map to those flags. Anything else is passed through to the real
command **without recovery** and a warning. Notable deliberate exclusions include
`cp --parents`, `cp -u`, `ln -f`, `ln -n`, and `rm -i`. `--` is supported.

## Design

State lives under `$UNDO_HOME`, default `~/.undo`. The state directory is kept
mode `0700`, the journal mode `0600`, and trash entry roots are `0700` because
they can contain sensitive file contents.

* `journal.jsonl`: one object per entry, with `id`, Unix `ts`, `cmd`, original
  `argv`, `cwd`, structured `inverse` operation lists, `trash` or null,
  `status`, and exit code. New entries also carry an `expected` signature map
  for copy-conflict checks.
* `journal.jsonl.bak`: the previous successfully written journal.
* `trash/<id>/<original absolute path without leading slash>`: recovery data.
* `progress/<id>`: small sidecar recording how many inverse operations have
  completed during an undo, so interrupted work can be resumed.
* `lock`: advisory lock serializing journal writers; readers see atomically replaced files.

Before a real command, `record CMD -- ...` computes the inverse, saves a pending
entry, and prints its id. The function runs `command CMD "$@"`, then invokes
`commit ID EXIT_CODE`. It returns the **real command's exit status**, not the
journal commit status. Unrecognized options/inverse failures warn and fall
through to the real command. Missing operands and unknown option syntaxes may
therefore not get entries. Normal commands incur two Python starts.

A successful command that changed nothing (`mkdir -p existing`, `cp -n`
existing destination, `mv -n` skipped, `rm -f` missing operand) is recorded as
`noop` and is skipped by default undo. An exact id still reports that nothing
was recorded.

Statuses are `pending` (recorded, awaiting the real command's exit code),
`done` (successful, undoable), `noop` (successful, no inverse), `failed` (the
real command or an inverse failed), `undoing` (an undo was interrupted), and
`undone` (already reversed). Only `done` entries are selected by default undo;
`failed`/`pending`/`undoing` are available to `--recover`.

Overwrite backups are **copies made before execution**, rather than moving the
destination away. This intentional interpretation of “stash” preserves the real
command's interactive/no-clobber/permission behavior. The inverse moves a moved
file back, then restores its old destination; a copy inverse removes the new
copy and restores the old destination. A skipped `mv` is detected at commit;
`cp -n` existing destinations have no inverse operations. Directory copies that
would merge an existing tree are deliberately not journaled. Backups are capped
by `UNDO_MAX_BACKUP_MB` (default 512), the 10,000-file path limit, and available
free space; exceeding a limit warns and runs the command unjournaled. Backing up
the journal itself or an ancestor of it is refused.

`cp` destinations are fingerprinted after the real command. Undo refuses to
delete a destination whose signature changed (inode, mode, size, mtime, ctime;
for a directory this catches entries being added or removed), unless `--force`
is used. This is a best-effort check, not a full content hash.

`rm` is special: it journals first and moves operands into trash using
`shutil.move` (copy/delete fallback across devices). It supports `-f`, `-r`/`-R`,
`-v`, combined flags, long equivalents, and `--`. Missing files are errors unless
forced; directories require recursion. Unknown options, including interactive
rm, warn and pass through to real rm **without recovery**, unless the fallback
would involve a root/`.`/`..` operand or `--no-preserve-root`; those unsafe
fallbacks are refused. `.`/`..` operands (including `foo/..`) are refused, as are
root, the current directory, and ancestors of the journal. Trash restoration
recreates missing parent directories with their current umask/default ownership,
not their former metadata, and refuses to overwrite an existing path.

`chmod` and `chown` save lstat modes (four-digit octal) or numeric uid:gid, walking
recursive trees without traversing symlink directories. More than 10,000 paths
causes a warning and unjournaled execution. Chown undo uses `os.lchown`.
`mkdir -p` records only absent directories, deepest first. `rmdir` recreates
empty directories, shallowest first; `ln` unlinks the new link.

Inverses are arrays, never evaluated shell strings. Undo stops on the first
failed inverse, marks the whole entry failed, and reports the failing operation.
It never continues blindly. Failed real commands are excluded from default undo,
even if they partially changed files, but their recorded inverses can be retried
with `undo --recover ID`. Undo progress is written to `progress/<id>` before the
next operation, and inverse operations are idempotent enough to retry safely.
Out-of-order exact-id undo is refused unless `--force` is used, because it can
apply stale state.

Journal updates use atomic replacement, file and directory fsync, and an
advisory lock. Automatic purge expires finished entries older than seven days;
failed/pending/undoing entries are retained for explicit recovery. `--purge`
expires all old entries, saves the journal before deleting trash, and sweeps
orphaned trash directories older than one day. `UNDO_RETENTION_DAYS` changes the
retention period.

## Environment

* `UNDO_HOME` — state directory, default `~/.undo`.
* `UNDO_PY` — engine path, default derived from the sourced script.
* `UNDO_DISABLE=1` — bypass the wrappers for one invocation.
* `UNDO_RETENTION_DAYS` — automatic retention, default `7`.
* `UNDO_MAX_BACKUP_MB` — overwrite-backup cap, default `512`; `0` means unlimited.
* `UNDO_DEBUG=1` — print tracebacks for engine errors.

## NOT covered / failure modes

* **No recovery for edits, redirects, editor saves, application writes, `sudo`
  commands, absolute command paths, or commands invoked outside these functions.**
* **Not transactional.** Journal writes are locked, but real file operations are
  not serialized against other processes. A crash between recording, real
  execution, and commit leaves a pending entry. Use `undo --recover` to inspect
  and retry; an interrupted undo can resume from `progress/<id>`. Partial undo is
  not rolled back automatically.
* **Do not modify files between the recorded command and undo.** In particular,
  undoing a copy deletes its destination (recursively if a directory). The
  signature check catches many changes but not content edits inside a directory
  that do not alter the root metadata. There are no content fingerprints or
  conflict merges.
* Special files, ACLs, hard-link relationships, sparse allocation, ownership on
  backups, and filesystem-specific metadata are not guaranteed to survive.
  Backups cost time and disk proportional to overwritten data.
* This parser deliberately supports a subset of command-line options. Unsupported
  modes (`--reference`, backup suffixes, `cp --parents`, `cp -u`, `mkdir -m`,
  `ln -f`, interactive `rm`, etc.) pass through with a warning. Copying onto a
  symlink or merging an existing directory is not journaled. Symbolic chmod
  operands starting with `-` need `--`. Chmod symlinks and dereferencing chown
  symlinks are not journaled to avoid restoring the wrong object. Restoring
  ownership may require privileges. Recursive chmod can make paths inaccessible,
  causing partial restoration.
* rm is not a complete GNU/BSD rm emulator: mount points, trailing slashes on
  symlinks, permission/sticky-directory checks, and operand overlap can differ.
  Trash moves can succeed where unlink would fail (or vice versa). `.`/`..`,
  root, the current directory, and ancestors of the journal are refused for
  safety, including on unjournaled fallbacks with `--no-preserve-root`. rm
  journal/storage failure after deletion begins is reported without retrying a
  possibly partially completed deletion. Initial journal failures, before any
  operand is touched, warn and fall through to real rm only when the fallback is
  safe. If the engine is missing entirely, wrappers warn and run the real
  commands, including real rm after a conservative shell-level unsafe-operand
  check. **A warning can mean no undo is possible.**
* rmdir undo recreates directories with current umask/default ownership, not the
  former metadata. ln overwrite flags are intentionally unsupported. No redo.
* An interactive cp decline may still have a harmless restore inverse, replacing
  the unchanged destination with its backup on undo. This can break hard links.
* Purging is irreversible, including failed/pending entries. There is no prompt,
  but `--purge` reports how many entries expired. Protect the state directory:
  the journal contains filenames and the trash contains file contents.
* Bash is integration-tested on Linux; zsh syntax is supported but not executed
  in the test suite. Code uses Python 3.9+ APIs only.

## Tests

```sh
bash tests/test_undo.sh
python3 -m unittest discover -s tests
```

Tests use isolated temporary HOME/UNDO_HOME values and never touch real user
files. Integration tests print PASS/FAIL for each case and return nonzero on any
failure. Unit tests cover inverse computation, backup ordering, journal status,
trash restoration/collisions, symlinks, argument parsing, recursion cap, purge,
dry-run, exact-id undo, unknown-option passthrough, no-op entries, `.`/`..`
refusal, rmdir ordering, corruption repair, permissions, backup limits, conflict
detection, out-of-order protection, partial-undo recovery, and concurrent journal writes.
