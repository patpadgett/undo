#!/usr/bin/env bash
set -u
export TMPDIR=${TMPDIR:-/tmp}
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TEST_ROOT=$(mktemp -d "$TMPDIR/undo-test.XXXXXXXX")
trap 'command rm -rf -- "$TEST_ROOT"' EXIT
export HOME="$TEST_ROOT/home" UNDO_HOME="$TEST_ROOT/journal"
command mkdir -p "$HOME" "$TEST_ROOT/work"
cd "$TEST_ROOT/work" || exit 1
source "$ROOT/undo.sh"
failures=0
assert() { if eval "$2"; then printf 'PASS %s\n' "$1"; else printf 'FAIL %s\n' "$1"; failures=$((failures+1)); fi; }
printf original > 'first file'
mv 'first file' 'second file'; undo -y >/dev/null
assert 'mv restores spaced filename' '[[ $(<"first file") == original && ! -e "second file" ]]'
printf old > dest
mv 'first file' dest; undo -y >/dev/null
assert 'mv overwrite restores both files' '[[ $(<"first file") == original && $(<dest) == old ]]'
rm 'first file'; undo -y >/dev/null
assert 'rm restores content' '[[ $(<"first file") == original ]]'
command mkdir -p tree/sub; printf leaf > tree/sub/file
rm -rf tree; undo -y >/dev/null
assert 'rm -rf restores tree' '[[ $(<tree/sub/file) == leaf ]]'
cp 'first file' dest; undo -y >/dev/null
assert 'cp overwrite restores old content' '[[ $(<dest) == old ]]'
command chmod 644 dest; chmod 600 dest; undo -y >/dev/null
assert 'chmod restores 644' '[[ $(stat -c %a dest) == 644 ]]'
mkdir newdir; undo -y >/dev/null
assert 'mkdir undone' '[[ ! -e newdir ]]'
assert 'undo list' '[[ $(undo -l 3) == *undone* ]]'
mv missing notcreated 2>/dev/null; rc=$?
assert 'failure exit propagates' '[[ $rc -ne 0 ]]'
assert 'failed command not undone' '[[ $(undo -l 1) == *failed* ]]'
assert 'no done entry remains' '! undo -y >/dev/null 2>&1'
UNDO_DISABLE=1 cp dest bypass
assert 'disabled command executes without journal' '[[ $(<bypass) == old && $(undo -l 1) == *failed* ]]'
mkdir -p nested/a/b; undo -y >/dev/null
assert 'mkdir -p removes new parents' '[[ ! -e nested ]]'
ln -s dest link; undo -y >/dev/null
assert 'ln undo unlinks' '[[ ! -L link && -e dest ]]'
command mkdir empty; rmdir empty; undo -y >/dev/null
assert 'rmdir undo recreates' '[[ -d empty ]]'
printf keep > skipdest; cp -n dest skipdest; undo -y >/dev/null
assert 'cp -n no-op undo preserves destination' '[[ $(<skipdest) == keep ]]'
rm -f absent
assert 'rm -f ignores missing' '[[ $? == 0 ]]'
rm tree 2>/dev/null; rc=$?
assert 'rm refuses directory' '[[ $rc -eq 1 && -d tree ]]'
printf 'Integration failures: %s\n' "$failures"
[[ $failures -eq 0 ]]
