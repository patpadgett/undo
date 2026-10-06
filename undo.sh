# Source this file; do not execute it. Bash and zsh are supported.
if [ -z "${UNDO_PY:-}" ]; then
    _undo_source=${BASH_SOURCE[0]:-${(%):-%x}}
    UNDO_PY="$(cd -- "$(dirname -- "$_undo_source")" && pwd)/undo.py"
    unset _undo_source
fi

_undo_wrap() {
    local _undo_cmd="$1" _undo_id='' _undo_rc=0
    shift
    if [ "${UNDO_DISABLE:-0}" = 1 ]; then
        command "$_undo_cmd" "$@"
        return $?
    fi
    if [ ! -r "$UNDO_PY" ] || ! command -v python3 >/dev/null 2>&1; then
        printf '%s\n' 'undo: not journaled (engine unavailable)' >&2
        command "$_undo_cmd" "$@"
        return $?
    fi
    if [ "$_undo_cmd" = rm ]; then
        if command python3 "$UNDO_PY" rm -- "$@"; then
            return 0
        else
            _undo_rc=$?
            # A journal failure may have occurred after a partial trash move.
            # Never run rm a second time in that case.
            return "$_undo_rc"
        fi
    fi
    if _undo_id=$(command python3 "$UNDO_PY" record "$_undo_cmd" -- "$@"); then
        :
    else
        _undo_id=''
    fi
    if command "$_undo_cmd" "$@"; then
        _undo_rc=0
    else
        _undo_rc=$?
    fi
    if [ -n "$_undo_id" ]; then
        command python3 "$UNDO_PY" commit "$_undo_id" "$_undo_rc" || :
    fi
    return "$_undo_rc"
}

mv() { _undo_wrap mv "$@"; }
cp() { _undo_wrap cp "$@"; }
rm() { _undo_wrap rm "$@"; }
chmod() { _undo_wrap chmod "$@"; }
chown() { _undo_wrap chown "$@"; }
mkdir() { _undo_wrap mkdir "$@"; }
rmdir() { _undo_wrap rmdir "$@"; }
ln() { _undo_wrap ln "$@"; }
undo() { command python3 "$UNDO_PY" "$@"; }
