# undo — journal-of-inverses shell undo (v0.1.0)

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

```sh
mv 'draft notes' 'final notes'
undo -n                  # inspect without changing anything
undo                     # show original command/inverses; undo? [y/N]
undo -y                  # reverse the last successful operation
undo -l                  # last 10 entries, including failed/undone entries
undo -l 25
undo --list 25
undo ENTRY_ID            # choose an exact id
undo -y ENTRY_ID
undo --purge             # permanently expire entries/trash older than 7 days
UNDO_DISABLE=1 mv a b    # bypass this invocation
command rm file          # bypass the shell function
```

**Bash/zsh detail:** `\rm` bypasses an *alias*, not a shell function; it does
NOT bypass these wrappers. Use `command rm`, `/bin/rm`, or `UNDO_DISABLE=1`.
Existing aliases can prevent these functions from being selected: remove
conflicting aliases before sourcing. These wrappers are intended for interactive
shells, not production scripts relying on exact GNU rm behavior.

## Design

State lives under `$UNDO_HOME`, default `~/.undo`:

* `journal.jsonl`: one object per entry, with `id`, Unix `ts`, `cmd`, original
  `argv`, `cwd`, structured `inverse` operation lists, `trash` or null, and
  `status` (`pending`, `done`, `undone`, `failed`).
* `trash/<id>/<original absolute path without leading slash>`: recovery data.

Before a real command, `record CMD -- ...` computes the inverse, saves a pending
entry, and prints its id. The function runs `command CMD "$@"`, then invokes
`commit ID EXIT_CODE`. It returns the **real command's exit status**, not the
journal commit status. Unrecognized options/inverse failures warn and fall
through to the real command. Missing operands and unknown option syntaxes may
therefore not get entries. Normal commands incur two Python starts.

Overwrite backups are **copies made before execution**, rather than moving the
destination away. This intentional interpretation of “stash” preserves the real
command's interactive/no-clobber/permission behavior. The inverse moves a moved
file back, then restores its old destination; a copy inverse removes the new
copy and restores the old destination. A skipped `mv` is detected at commit;
`cp -n` existing destinations have no inverse operations. Directory copies that
would merge an existing tree are deliberately not journaled.

`rm` is special: it journals first and moves operands into trash using
`shutil.move` (copy/delete fallback across devices). It supports `-f`, `-r`/`-R`,
`-v`, combined flags, long equivalents, and `--`. Missing files are errors unless
forced; directories require recursion. Unknown options, including interactive
rm, warn and pass through to real rm **without recovery**. Trash restoration
recreates missing parent directories and refuses to overwrite an existing path.

`chmod` and `chown` save lstat modes (four-digit octal) or numeric uid:gid, walking
recursive trees without traversing symlink directories. More than 10,000 paths
causes a warning and unjournaled execution. Chown undo uses `os.lchown`.
`mkdir -p` records only absent directories, deepest first. `rmdir` recreates
empty directories; `ln` unlinks the new link.

Inverses are arrays, never evaluated shell strings. Undo stops on the first
failed inverse, marks the whole entry failed, and reports the failing operation.
It never continues blindly. Failed real commands are excluded from default undo,
even if they partially changed files. Journal updates use atomic replacement
and fsync. Each new recorded command cheaply scans timestamps and automatically
purges recovery data older than seven days.

## NOT covered / failure modes

* **No recovery for edits, redirects, editor saves, application writes, `sudo`
  commands, absolute command paths, or commands invoked outside these functions.**
* **Not transactional, not concurrency-safe.** Use one shell/operation at a time.
  Separate simultaneous processes can race or lose journal updates. A crash
  between recording, real execution, and commit leaves a pending entry. No
  automatic recovery is attempted for pending or failed entries; inspect the
  JSON and trash manually. Partial undo is not rolled back or retryable via CLI.
* **Do not modify files between the recorded command and undo.** In particular,
  undoing a copy deletes its destination (recursively if a directory), including
  later changes. There are no content fingerprints or conflict merges. A later
  replacement of an unrelated path cannot always be detected.
* Special files, ACLs, hard-link relationships, sparse allocation, ownership on
  backups, and filesystem-specific metadata are not guaranteed to survive.
  Backups cost time and disk proportional to overwritten data; no size quota.
* This parser deliberately supports a subset of command-line options. Unsupported
  modes (`--reference`, backup suffixes, mkdir modes, etc.) pass through with a
  warning. Copying onto a symlink or merging an existing directory is not
  journaled. Symbolic chmod operands starting with `-` need `--`. Chmod symlinks
  and dereferencing chown symlinks are not journaled to avoid restoring the wrong
  object. Restoring ownership may require privileges. Recursive chmod can make
  paths inaccessible, causing partial restoration.
* rm is not a complete GNU/BSD rm emulator: mount points, trailing slashes on
  symlinks, permission/sticky-directory checks, and operand overlap can differ.
  Trash moves can succeed where unlink would fail (or vice versa). Root, the
  current directory, and ancestors of the journal are refused for safety.
  rm journal/storage failure after deletion begins is reported without retrying a
  possibly partially completed deletion. Initial journal failures, before any
  operand is touched, warn and fall through to real rm. If the engine is missing
  entirely, wrappers warn and run
  the real commands, including real rm. **A warning can mean no undo is possible.**
* rmdir undo recreates directories with current umask/default ownership, not the
  former metadata. ln overwrite flags are intentionally unsupported. No redo.
* An interactive cp decline may still have a harmless restore inverse, replacing
  the unchanged destination with its backup on undo. This can break hard links.
* Purging is irreversible, including failed/pending entries. There is no prompt.
  Protect the journal: it contains filenames and potentially sensitive contents.
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
dry-run, exact-id undo, and unknown-option passthrough.
