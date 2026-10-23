#!/usr/bin/env python3
"""Best-effort shell journal of structured inverse operations (stdlib only)."""
import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - this tool targets Unix shells
    fcntl = None


VERSION = '0.2.1'
RETENTION_DAYS_DEFAULT = 7.0
MAX_PATHS = 10000
MAX_BACKUP_FILES = 10000
BACKUP_LIMIT_MB_DEFAULT = 512.0
DEFAULT_LIST = 10
ID_RE = re.compile(r'^[0-9a-f]{32}$')


class NotJournaled(Exception):
    pass


class JournalError(Exception):
    pass


class JournalCorrupt(JournalError):
    def __init__(self, path, line, detail):
        self.path = str(path)
        self.line = line
        self.detail = detail
        super().__init__('corrupt journal {} at line {}: {}'.format(self.path, line, detail))


class ConflictError(JournalError):
    pass


def home():
    return Path(os.environ.get('UNDO_HOME', os.path.expanduser('~/.undo'))).absolute()


def journal_path():
    return home() / 'journal.jsonl'


def backup_path():
    return home() / 'journal.jsonl.bak'


def exists(path):
    return os.path.lexists(path)


def absolute(path):
    return os.path.abspath(path)


def retention_seconds():
    raw = os.environ.get('UNDO_RETENTION_DAYS', str(RETENTION_DAYS_DEFAULT))
    try:
        days = float(raw)
    except ValueError:
        days = RETENTION_DAYS_DEFAULT
    return max(0.0, days) * 86400


def backup_limit_bytes():
    raw = os.environ.get('UNDO_MAX_BACKUP_MB', str(BACKUP_LIMIT_MB_DEFAULT))
    try:
        megabytes = float(raw)
    except ValueError:
        megabytes = BACKUP_LIMIT_MB_DEFAULT
    if megabytes <= 0:
        return 0  # Explicitly unlimited; documented as unsafe.
    return int(megabytes * 1024 * 1024)


def ensure_private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(path), 0o700)
    return path


def ensure_home():
    return ensure_private_dir(home())


def fsync_dir(path):
    flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


_lock_depth = 0


@contextlib.contextmanager
def locked():
    """Serialize journal updates across processes; reentrant in one process."""
    global _lock_depth
    if _lock_depth:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    state = ensure_home()
    lock = state / 'lock'
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, 'O_CLOEXEC'):
        flags |= os.O_CLOEXEC
    fd = os.open(str(lock), flags, 0o600)
    try:
        try:
            os.chmod(str(lock), 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        _lock_depth = 1
        try:
            yield
        finally:
            _lock_depth = 0
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def validate_entry(entry):
    if not isinstance(entry, dict):
        return False
    ident = entry.get('id')
    if not isinstance(ident, str) or not ID_RE.match(ident):
        return False
    if not isinstance(entry.get('ts'), (int, float)):
        return False
    if not isinstance(entry.get('argv'), list):
        return False
    if not isinstance(entry.get('inverse'), list):
        return False
    if not isinstance(entry.get('status'), str):
        return False
    return True


def load(path=None):
    source = Path(path) if path is not None else journal_path()
    try:
        stream = source.open('rb')
    except FileNotFoundError:
        return []
    entries = []
    with stream:
        for lineno, raw in enumerate(stream, 1):
            try:
                line = raw.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise JournalCorrupt(source, lineno, 'invalid UTF-8') from exc
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalCorrupt(source, lineno, 'invalid JSON ({})'.format(exc.msg)) from exc
            if not validate_entry(entry):
                raise JournalCorrupt(source, lineno, 'invalid journal entry')
            entries.append(entry)
    return entries


def save(entries, backup=True):
    state = ensure_home()
    temp = state / ('journal.' + uuid.uuid4().hex + '.tmp')
    journal = state / 'journal.jsonl'
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        fd = os.open(str(temp), flags, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            for entry in entries:
                stream.write(json.dumps(entry) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        if backup and journal.exists():
            old = state / ('journal.bak.' + uuid.uuid4().hex + '.tmp')
            try:
                try:
                    os.link(str(journal), str(old))
                except OSError:
                    shutil.copy2(str(journal), str(old))
                os.chmod(str(old), 0o600)
                os.replace(str(old), str(backup_path()))
            except OSError:
                try:
                    old.unlink()
                except OSError:
                    pass
        os.replace(str(temp), str(journal))
        fsync_dir(state)
    finally:
        if temp.exists():
            temp.unlink()


def purge(include_unfinished=False, sweep_orphans=False):
    with locked():
        entries = load()
        cutoff = time.time() - retention_seconds()
        expired, kept = [], []
        for entry in entries:
            status = entry.get('status')
            unfinished = status not in ('done', 'noop', 'undone')
            if entry.get('ts', 0) < cutoff and (include_unfinished or not unfinished):
                expired.append(entry)
            else:
                kept.append(entry)
        if expired:
            # The journal is updated first so a crash cannot leave entries
            # pointing at recovery data that has already been deleted.
            save(kept)
            for entry in expired:
                shutil.rmtree(str(home() / 'trash' / entry['id']), ignore_errors=True)
                clear_progress(entry['id'])
        if sweep_orphans:
            known = {entry['id'] for entry in entries}
            trash = home() / 'trash'
            if trash.is_dir():
                cutoff_orphan = time.time() - 86400
                for child in trash.iterdir():
                    try:
                        if child.name not in known and child.stat().st_mtime < cutoff_orphan:
                            shutil.rmtree(str(child), ignore_errors=True)
                    except OSError:
                        pass
        return expired


def parse_args(cmd, argv):
    operands, flags, target = [], set(), None
    i, options = 0, True
    short = {'mv': 'fivnT', 'cp': 'fivnTrRpPadLHls', 'rm': 'frRv',
             'chmod': 'Rvcf', 'chown': 'Rvcfh', 'mkdir': 'pv',
             'rmdir': 'v', 'ln': 'siv'}[cmd]
    longs = {'--recursive': 'R', '--force': 'f', '--verbose': 'v',
             '--interactive': 'i', '--no-clobber': 'n', '--parents': 'p',
             '--symbolic': 's', '--no-target-directory': 'T'}
    if cmd == 'cp':
        # cp -p means preserve, not --parents.  --parents changes where files
        # are created, so it cannot share the simple inverse logic.
        longs.pop('--parents', None)
        longs['--archive'] = 'a'
        longs['--dereference'] = 'L'
        longs['--no-dereference'] = 'P'
        longs['--link'] = 'l'
        longs['--symbolic-link'] = 's'
    if cmd == 'chown':
        longs['--no-dereference'] = 'h'
    while i < len(argv):
        arg = argv[i]
        if options and arg == '--':
            options = False
        elif options and cmd in ('mv', 'cp') and (arg in ('-t', '--target-directory') or arg.startswith('--target-directory=') or arg.startswith('-t')):
            if arg in ('-t', '--target-directory'):
                i += 1
                if i == len(argv):
                    raise NotJournaled('missing target directory')
                target = argv[i]
            else:
                target = arg.split('=', 1)[1] if arg.startswith('--') else arg[2:]
        elif options and arg.startswith('-') and arg != '-':
            if arg in longs and longs[arg] in short:
                flags.add(longs[arg])
            elif not arg.startswith('--') and all(c in short for c in arg[1:]):
                flags.update(arg[1:])
            else:
                raise NotJournaled('unknown option ' + arg)
        else:
            operands.append(arg)
        i += 1
    return operands, flags, target


def new_entry(cmd, argv) -> dict:
    return dict(id=uuid.uuid4().hex, ts=time.time(), cmd=cmd, argv=argv,
                cwd=os.getcwd(), inverse=[], trash=None, status='pending',
                exit=None)


def ensure_entry_trash(ident):
    return ensure_private_dir(ensure_private_dir(home() / 'trash') / ident)


def contains_path(parent, child):
    try:
        parent = os.path.realpath(parent)
        child = os.path.realpath(child)
        return os.path.commonpath([parent, child]) == parent
    except (OSError, ValueError):
        return False


def copy_regular(src, dst):
    info = os.lstat(src)
    if not stat.S_ISREG(info.st_mode):
        raise NotJournaled('cannot back up special file {}'.format(src))
    shutil.copy2(src, dst)


def scan_backup(path):
    count, total = 0, 0
    limit = backup_limit_bytes()
    if os.path.islink(path):
        return 1, 0
    info = os.lstat(path)
    if stat.S_ISREG(info.st_mode):
        if limit and info.st_size > limit:
            raise NotJournaled('backup exceeds UNDO_MAX_BACKUP_MB')
        return 1, info.st_size
    if not stat.S_ISDIR(info.st_mode):
        raise NotJournaled('cannot back up special file {}'.format(path))
    for root, dirs, files in os.walk(path, followlinks=False):
        count += 1
        for name in dirs + files:
            item = os.path.join(root, name)
            info = os.lstat(item)
            if stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode):
                count += 1
            elif stat.S_ISREG(info.st_mode):
                count += 1
                total += info.st_size
            else:
                raise NotJournaled('cannot back up special file {}'.format(item))
            if count > MAX_BACKUP_FILES:
                raise NotJournaled('backup contains too many files')
            if limit and total > limit:
                raise NotJournaled('backup exceeds UNDO_MAX_BACKUP_MB')
    return count, total


def stash(entry, path):
    path = os.path.abspath(path)
    state = ensure_home()
    if contains_path(path, str(state)):
        raise NotJournaled('refusing to back up the journal or its ancestor')
    _, total = scan_backup(path)
    free = shutil.disk_usage(str(state)).free
    if total + 64 * 1024 * 1024 > free:
        raise NotJournaled('not enough free space for backup')
    root = ensure_entry_trash(entry['id'])
    dest = root / path.lstrip(os.sep)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if exists(str(dest)):
        raise NotJournaled('duplicate backup destination')
    # Copy, not rename: preserve the real command's -i/-n and failure semantics.
    if os.path.islink(path):
        os.symlink(os.readlink(path), str(dest))
    elif os.path.isdir(path):
        shutil.copytree(path, str(dest), symlinks=True, copy_function=copy_regular)
    elif stat.S_ISREG(os.lstat(path).st_mode):
        shutil.copy2(path, str(dest))
    else:
        raise NotJournaled('cannot back up special file')
    entry['trash'] = str(root)
    return str(dest)


def affected(paths, recursive):
    result = []
    seen = set()
    for path in paths:
        candidates = [path]
        if recursive and not os.path.islink(path) and os.path.isdir(path):
            for root, dirs, files in os.walk(path, followlinks=False):
                candidates.extend(os.path.join(root, name) for name in dirs + files)
                if len(candidates) > MAX_PATHS:
                    raise NotJournaled('too many files')
        for item in candidates:
            if item not in seen:
                seen.add(item)
                result.append(item)
                if len(result) > MAX_PATHS:
                    raise NotJournaled('too many files')
    return result


def compute_inverse(entry):
    cmd = entry['cmd']
    args, flags, target = parse_args(cmd, entry['argv'])
    ops = []
    if cmd in ('mv', 'cp', 'ln'):
        if target is not None:
            sources, dest = args, absolute(target)
        elif len(args) >= 2:
            sources, dest = args[:-1], absolute(args[-1])
        elif cmd == 'ln' and len(args) == 1:
            sources, dest = args, absolute('.')
        else:
            raise NotJournaled('missing operands')
        if not sources:
            raise NotJournaled('missing source')
        if len(sources) > 1 and not os.path.isdir(dest):
            raise NotJournaled('multiple sources require a directory')
        pairs = []
        for source in sources:
            src = absolute(source)
            if cmd in ('mv', 'cp') and not exists(src):
                raise NotJournaled('missing source {}'.format(source))
            dst = os.path.join(dest, os.path.basename(src)) if os.path.isdir(dest) and 'T' not in flags else dest
            if src == dst or (exists(src) and exists(dst) and os.path.samefile(src, dst)):
                raise NotJournaled('source and destination are identical')
            if cmd == 'cp' and os.path.islink(dst):
                raise NotJournaled('copy through destination symlink')
            if cmd == 'cp' and os.path.isdir(dst):
                raise NotJournaled('copy merges existing directory')
            if any(dst == other[1] for other in pairs):
                raise NotJournaled('duplicate destinations')
            pairs.append((src, dst))
        for src, dst in pairs:
            if cmd == 'ln':
                if exists(dst):
                    raise NotJournaled('link destination exists')
                ops.append(['unlink', dst])
                continue
            if 'n' in flags and exists(dst):
                continue
            backup = stash(entry, dst) if exists(dst) else None
            if cmd == 'mv':
                ops.append(['mv', dst, src])
                if backup:
                    ops.append(['restore', backup, dst])
            else:
                ops.append(['rm', dst])
                if backup:
                    ops.append(['restore', backup, dst])
    elif cmd in ('chmod', 'chown'):
        if len(args) < 2:
            raise NotJournaled('missing operands')
        for path in affected([absolute(p) for p in args[1:]], 'R' in flags):
            info = os.lstat(path)
            if cmd == 'chmod':
                if stat.S_ISLNK(info.st_mode):
                    raise NotJournaled('chmod symlink target cannot be tracked safely')
                ops.append(['chmod', format(stat.S_IMODE(info.st_mode), '04o'), path])
            else:
                if stat.S_ISLNK(info.st_mode) and 'h' not in flags:
                    raise NotJournaled('chown symlink target cannot be tracked safely')
                ops.append(['chown', '{}:{}'.format(info.st_uid, info.st_gid), path])
    elif cmd == 'mkdir':
        paths = set()
        for arg in args:
            path = absolute(arg)
            while not exists(path):
                paths.add(path)
                if 'p' not in flags:
                    break
                path = os.path.dirname(path)
        ops = [['rmdir', p] for p in sorted(paths, key=lambda p: (p.count(os.sep), p), reverse=True)]
    elif cmd == 'rmdir':
        paths = sorted({p for p in (absolute(arg) for arg in args)
                        if os.path.isdir(p) and not os.path.islink(p)},
                       key=lambda p: (p.count(os.sep), p))
        ops = [['mkdir', p] for p in paths]
    entry['inverse'] = ops
    return ops


def clear_progress(ident):
    try:
        (home() / 'progress' / ident).unlink()
    except OSError:
        pass


def record(cmd, argv):
    purge()
    entry = new_entry(cmd, argv)
    try:
        compute_inverse(entry)
        with locked():
            entries = load()
            entries.append(entry)
            save(entries)
    except BaseException:
        shutil.rmtree(str(home() / 'trash' / entry['id']), ignore_errors=True)
        clear_progress(entry['id'])
        raise
    print(entry['id'])


def signature(path):
    info = os.lstat(path)
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


def capture_expected(entry):
    if entry.get('cmd') != 'cp':
        return
    expected = {}
    for op in entry.get('inverse', []):
        if isinstance(op, list) and len(op) >= 2 and op[0] == 'rm':
            path = op[1]
            if exists(path):
                try:
                    expected[path] = signature(path)
                except OSError:
                    pass
    if expected:
        entry['expected'] = expected
    else:
        entry.pop('expected', None)


def commit(ident, code):
    with locked():
        entries = load()
        entry = next((item for item in entries if item['id'] == ident), None)
        if entry is None:
            raise JournalError('journal entry not found: ' + ident)
        entry['status'] = 'done' if code == 0 else 'failed'
        entry['exit'] = code
        if code == 0 and entry.get('cmd') == 'mv':
            filtered, skip_restore = [], False
            for op in entry.get('inverse', []):
                if op and op[0] == 'mv':
                    skip_restore = exists(op[2])
                    if not skip_restore:
                        filtered.append(op)
                elif not skip_restore:
                    filtered.append(op)
            entry['inverse'] = filtered
        if code == 0 and not entry.get('inverse'):
            entry['status'] = 'noop'
        capture_expected(entry)
        save(entries)


def _is_dot_operand(operand):
    tail = operand.rstrip('/')
    if not tail:
        return False
    return os.path.basename(tail) in ('.', '..')


def _unsafe_rm_path(path):
    path = os.path.realpath(absolute(path))
    state = os.path.realpath(str(home()))
    return (path == '/' or path == os.path.realpath(os.getcwd())
            or state == path or state.startswith(path + os.sep))


def _rm_fallback_allowed(argv):
    """Never let an unjournaled fallback bypass the root/dot safety checks."""
    for arg in argv:
        if arg == '--no-preserve-root':
            return False
        if arg.startswith('--preserve-root=') and arg.split('=', 1)[1] == 'no':
            return False
        if arg == '--' or (arg.startswith('-') and arg != '-'):
            continue
        if _is_dot_operand(arg) or _unsafe_rm_path(arg):
            return False
    return True


def trash_rm(argv):
    try:
        paths, flags, _ = parse_args('rm', argv)
    except NotJournaled as exc:
        print('undo: not journaled ({})'.format(exc), file=sys.stderr)
        if not _rm_fallback_allowed(argv):
            print('undo: refusing unjournaled rm with an unsafe root/dot operand', file=sys.stderr)
            return 2
        return subprocess.call(['rm'] + argv)
    if not paths:
        if 'f' in flags:
            return 0
        print('rm: missing operand', file=sys.stderr)
        return 1
    journal_error = None
    with locked():
        try:
            purge()
            entry = new_entry('rm', argv)
            entries = load()
            entries.append(entry)
            save(entries)
        except Exception as exc:
            journal_error = exc
        else:
            code = 0
            try:
                for operand in paths:
                    if _is_dot_operand(operand):
                        print("rm: refusing to remove '.' or '..' directory: skipping '{}'".format(operand), file=sys.stderr)
                        code = 1
                        continue
                    path = absolute(operand)
                    if not exists(path):
                        if 'f' not in flags:
                            print("rm: cannot remove '{}': No such file or directory".format(operand), file=sys.stderr)
                            code = 1
                        continue
                    if os.path.isdir(path) and not os.path.islink(path) and not ({'r', 'R'} & flags):
                        print("rm: cannot remove '{}': Is a directory".format(operand), file=sys.stderr)
                        code = 1
                        continue
                    # Refuse catastrophically broad paths, including the live journal.
                    if _unsafe_rm_path(path):
                        print("undo: refusing unsafe trash path '{}'".format(path), file=sys.stderr)
                        code = 1
                        continue
                    root = ensure_entry_trash(entry['id'])
                    dest = root / path.lstrip(os.sep)
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if exists(str(dest)):
                            raise OSError('duplicate trash destination')
                        entry['trash'] = str(root)
                        entry['inverse'].insert(0, ['restore', str(dest), path])
                        try:
                            save(entries)  # Persist inverse before touching the operand.
                        except Exception as exc:
                            entry['inverse'].pop(0)
                            print('undo: journal update failed: {}'.format(exc), file=sys.stderr)
                            code = 1
                            break
                        shutil.move(path, str(dest))
                        if 'v' in flags:
                            print("removed '{}'".format(operand))
                    except OSError as exc:
                        print('rm: {}: {}'.format(operand, exc), file=sys.stderr)
                        code = 1
            except KeyboardInterrupt:
                entry['status'] = 'failed'
                entry['error'] = 'interrupted'
                entry['exit'] = 130
                try:
                    save(entries)
                except Exception:
                    pass
                raise
            if code == 0 and not entry['inverse']:
                entry['status'] = 'noop'
            else:
                entry['status'] = 'done' if code == 0 else 'failed'
            entry['exit'] = code
            try:
                save(entries)
            except Exception as exc:
                print('undo: commit failed ({}); the command completed but may not be undoable'.format(exc), file=sys.stderr)
            return code
    print('undo: not journaled ({})'.format(journal_error), file=sys.stderr)
    if not _rm_fallback_allowed(argv):
        print('undo: refusing unjournaled rm with an unsafe root/dot operand', file=sys.stderr)
        return 2
    # No operand has been touched: a real-rm fallback is safe here.
    return subprocess.call(['rm'] + argv)


def run_op(op):
    kind = op[0]
    if kind in ('mv', 'restore'):
        src, dst = op[1:]
        if not exists(src):
            if exists(dst):
                return  # Already applied by an interrupted undo.
            raise FileNotFoundError(src)
        if exists(dst):
            raise FileExistsError('target now exists: ' + dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
    elif kind == 'rm':
        path = op[1]
        if not exists(path):
            return
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    elif kind == 'unlink':
        if not exists(op[1]):
            return
        os.unlink(op[1])
    elif kind == 'rmdir':
        if not exists(op[1]):
            return
        os.rmdir(op[1])
    elif kind == 'mkdir':
        path = op[1]
        if exists(path):
            if os.path.isdir(path) and not os.path.islink(path):
                return
            raise FileExistsError('target now exists: ' + path)
        os.makedirs(path, exist_ok=False)
    elif kind == 'chmod':
        if not exists(op[2]):
            raise FileNotFoundError(op[2])
        if os.path.islink(op[2]):
            raise OSError('refusing to chmod a replacement symlink')
        os.chmod(op[2], int(op[1], 8))
    elif kind == 'chown':
        if not exists(op[2]):
            raise FileNotFoundError(op[2])
        uid, gid = map(int, op[1].split(':'))
        os.lchown(op[2], uid, gid)
    else:
        raise ValueError('unknown inverse ' + kind)


def progress_dir():
    return ensure_private_dir(home() / 'progress')


def progress_path(ident):
    return progress_dir() / ident


def read_progress(ident):
    try:
        return max(0, int(progress_path(ident).read_text().strip()))
    except (OSError, ValueError):
        return 0


def write_progress(ident, index, sync=False):
    directory = progress_dir()
    temp = directory / (ident + '.tmp')
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        fd = os.open(str(temp), flags, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write('{}\n'.format(int(index)))
            stream.flush()
            if sync:
                os.fsync(stream.fileno())
        os.replace(str(temp), str(directory / ident))
        if sync:
            fsync_dir(directory)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def describe(entry, start=0):
    print('{}: {} (cwd {})'.format(entry['id'], shlex.join([entry.get('cmd', '?')] + entry.get('argv', [])), entry.get('cwd', '?')))
    status = entry.get('status')
    if status != 'done':
        print('  status: {}'.format(status))
    if entry.get('error'):
        print('  error: {}'.format(entry['error']))
    words = {'mv': 'move', 'restore': 'restore from trash', 'rm': 'remove copy',
             'unlink': 'unlink', 'rmdir': 'remove empty directory', 'mkdir': 'recreate directory',
             'chmod': 'restore mode', 'chown': 'restore owner'}
    ops = entry.get('inverse', [])
    if not ops:
        print('  no filesystem changes recorded')
        return
    shown = 0
    for index, op in enumerate(ops):
        if index < start:
            continue
        if shown >= 50:
            print('  ... {} more operation(s)'.format(len(ops) - index))
            break
        print('  {}: {}'.format(words.get(op[0], op[0]), ' -> '.join(repr(p) for p in op[1:])))
        shown += 1


def conflict_paths(entry):
    changed = []
    expected = entry.get('expected') or {}
    if not isinstance(expected, dict):
        return changed
    for path, expected_signature in expected.items():
        if not exists(path):
            continue
        try:
            current = signature(path)
        except OSError:
            changed.append(path)
            continue
        if current != expected_signature:
            changed.append(path)
    return changed


def apply_entry(ident, force=False, recover=False, require_latest=False):
    with locked():
        entries = load()
        entry = next((item for item in entries if item['id'] == ident), None)
        if entry is None:
            print('undo: no entry with id {}'.format(ident), file=sys.stderr)
            return 1
        status = entry.get('status')
        ops = entry.get('inverse', [])
        if status == 'noop' or (status == 'done' and not ops):
            print('undo: entry {} recorded no changes; nothing to undo'.format(ident))
            return 0
        if recover:
            if status not in ('failed', 'pending', 'undoing'):
                print('undo: entry {} is {}; --recover only applies to failed/pending/undoing entries'.format(ident, status), file=sys.stderr)
                return 1
        elif status != 'done':
            print('undo: entry {} is {}; use --recover to retry'.format(ident, status), file=sys.stderr)
            return 1
        if not ops:
            if recover:
                entry['status'] = 'noop'
                save(entries)
                print('undo: entry {} has no recorded inverses; marked noop'.format(ident))
                return 0
            print('undo: entry {} recorded no changes; nothing to undo'.format(ident))
            return 0
        if require_latest and not force and not recover:
            position = entries.index(entry)
            newer = [other for other in entries[position + 1:]
                     if other.get('status') in ('done', 'failed', 'pending', 'undoing')
                     and other.get('inverse')]
            if newer:
                noun = 'entry' if len(newer) == 1 else 'entries'
                print('undo: refusing out-of-order undo; {} newer recorded {} exist. Use --force or resolve them first.'.format(len(newer), noun), file=sys.stderr)
                return 1
        start = read_progress(ident) if recover or status in ('failed', 'pending', 'undoing') else 0
        start = max(0, min(start, len(ops)))
        if not force:
            changed = conflict_paths(entry)
            if changed:
                print('undo: refusing to undo because paths changed after the command:', file=sys.stderr)
                for path in changed:
                    print('  {}'.format(path), file=sys.stderr)
                print('undo: re-run with --force to override', file=sys.stderr)
                return 1
        entry['status'] = 'undoing'
        entry['progress'] = start
        entry.pop('error', None)
        entry.pop('failed_op', None)
        save(entries)
        write_progress(ident, start, sync=True)
        index = start
        try:
            while index < len(ops):
                run_op(ops[index])
                index += 1
                # Fsync periodically: a crash may repeat a few idempotent
                # operations, but not hundreds of expensive fsyncs.
                write_progress(ident, index, sync=(index % 100 == 0 or index == len(ops)))
        except KeyboardInterrupt:
            entry['status'] = 'failed'
            entry['error'] = 'interrupted'
            entry['failed_op'] = index
            entry['progress'] = index
            try:
                write_progress(ident, index, sync=True)
            except OSError:
                pass
            save(entries)
            print('undo: interrupted at operation {}; use --recover {}'.format(index, ident), file=sys.stderr)
            return 130
        except Exception as exc:
            entry['status'] = 'failed'
            entry['error'] = str(exc)
            entry['failed_op'] = index
            entry['progress'] = index
            try:
                write_progress(ident, index, sync=True)
            except OSError:
                pass
            save(entries)
            failed = ops[index] if index < len(ops) else 'progress update'
            print('undo: inverse failed {}: {}'.format(failed, exc), file=sys.stderr)
            print('undo: fix the cause and re-run: undo --recover {}'.format(ident), file=sys.stderr)
            return 1
        entry['status'] = 'undone'
        entry['undo_ts'] = time.time()
        entry.pop('error', None)
        entry.pop('failed_op', None)
        entry.pop('progress', None)
        save(entries)
        clear_progress(ident)
        return 0


def doctor():
    state = home()
    print('undo home: {}'.format(state))
    if not state.exists():
        print('state: not initialized')
        return 0
    issues = 0
    mode = stat.S_IMODE(os.stat(str(state)).st_mode)
    print('home mode: {:04o}'.format(mode))
    if mode & 0o077:
        print('warning: UNDO_HOME is accessible by group/others')
        issues += 1
    journal = journal_path()
    if not journal.exists():
        print('journal: absent')
        return 0 if issues == 0 else 1
    mode = stat.S_IMODE(os.stat(str(journal)).st_mode)
    print('journal mode: {:04o}'.format(mode))
    if mode & 0o077:
        print('warning: journal is accessible by group/others')
        issues += 1
    try:
        entries = load()
    except JournalCorrupt as exc:
        print('journal: CORRUPT: {}'.format(exc))
        print("run 'undo --repair' to preserve the bad file and salvage valid entries")
        return 1
    print('journal: ok ({} entries)'.format(len(entries)))
    counts = {}
    for entry in entries:
        status = entry.get('status', 'unknown')
        counts[status] = counts.get(status, 0) + 1
    print('status: {}'.format(', '.join('{}={}'.format(k, counts[k]) for k in sorted(counts)) or 'none'))
    unfinished = [entry for entry in entries if entry.get('status') in ('failed', 'pending', 'undoing')]
    if unfinished:
        print('recoverable: {}'.format(', '.join('{} ({})'.format(entry['id'], entry.get('status')) for entry in unfinished[-10:])))
    trash = state / 'trash'
    if trash.is_dir():
        known = {entry['id'] for entry in entries}
        dirs = [child for child in trash.iterdir() if child.is_dir()]
        orphans = [child for child in dirs if child.name not in known]
        print('trash: {} entries, {} orphaned'.format(len(dirs), len(orphans)))
    print('backup: {}'.format('present' if backup_path().exists() else 'absent'))
    return 0 if issues == 0 else 1


def repair_journal():
    with locked():
        state = ensure_home()
        journal = journal_path()
        if not journal.exists():
            print('undo: no journal to repair')
            return 0
        try:
            load()
            print('undo: journal is valid; no repair needed')
            return 0
        except JournalCorrupt:
            pass
        stamp = time.strftime('%Y%m%d-%H%M%S')
        bad = state / ('journal.jsonl.corrupt-' + stamp)
        os.replace(str(journal), str(bad))
        valid, skipped = [], 0
        with bad.open('rb') as stream:
            for raw in stream:
                try:
                    line = raw.decode('utf-8')
                except UnicodeDecodeError:
                    skipped += 1
                    continue
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if validate_entry(entry):
                    valid.append(entry)
                else:
                    skipped += 1
        save(valid, backup=False)
        print('undo: repaired journal; kept {} entries, skipped {}; original: {}'.format(len(valid), skipped, bad))
        return 0


def undo(args):
    if args.repair:
        return repair_journal()
    if args.doctor:
        return doctor()
    if args.purge:
        with locked():
            expired = purge(include_unfinished=True, sweep_orphans=True)
        noun = 'entry' if len(expired) == 1 else 'entries'
        print('undo: purged {} journal {}.'.format(len(expired), noun))
        return 0
    entries = load()
    if args.list is not None:
        for entry in entries[-args.list:] if args.list else []:
            age = max(0, int(time.time() - entry['ts']))
            print('{} {}s {} {}'.format(entry['id'], age, entry.get('status'), shlex.join([entry.get('cmd', '?')] + entry.get('argv', []))))
        return 0
    statuses = ('failed', 'pending', 'undoing') if args.recover else ('done',)
    if args.id:
        target = next((entry for entry in entries if entry['id'] == args.id), None)
        if target is None:
            print('undo: no entry with id {}'.format(args.id), file=sys.stderr)
            return 1
        status = target.get('status')
        if args.recover:
            if status not in statuses:
                print('undo: entry {} is {}; --recover only applies to failed/pending/undoing entries'.format(args.id, status), file=sys.stderr)
                return 1
        elif status == 'noop' or (status == 'done' and not target.get('inverse')):
            print('undo: entry {} recorded no changes; nothing to undo'.format(args.id))
            return 0
        elif status == 'undone':
            print('undo: entry {} has already been undone'.format(args.id), file=sys.stderr)
            return 1
        elif status != 'done':
            print('undo: entry {} is {}; use --recover to retry'.format(args.id, status), file=sys.stderr)
            return 1
    else:
        candidates = [entry for entry in entries
                      if entry.get('status') in statuses and (args.recover or entry.get('inverse'))]
        if not candidates:
            if args.recover:
                print('undo: no recoverable failed/pending entries', file=sys.stderr)
            else:
                print('undo: no matching done entry', file=sys.stderr)
            return 1
        target = candidates[-1]
    index = entries.index(target)
    if not args.recover and args.id and not args.force:
        newer = [entry for entry in entries[index + 1:]
                 if entry.get('status') in ('done', 'failed', 'pending', 'undoing')
                 and entry.get('inverse')]
        if newer:
            noun = 'entry' if len(newer) == 1 else 'entries'
            print('undo: refusing out-of-order undo; {} newer recorded {} exist. Use --force or resolve them first.'.format(len(newer), noun), file=sys.stderr)
            return 1
    start = read_progress(target['id']) if args.recover else 0
    if args.recover:
        print('undo: recovering {} entry {} (status {})'.format(target.get('status'), target['id'], target.get('status')))
    describe(target, start=start)
    if args.dry_run:
        return 0
    if not args.yes:
        try:
            answer = input('undo? [y/N] ')
        except EOFError:
            print('undo: no confirmation available on stdin', file=sys.stderr)
            return 1
        if answer.strip().lower() not in ('y', 'yes'):
            return 0
    return apply_entry(target['id'], force=args.force, recover=args.recover,
                       require_latest=bool(args.id))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ('record', 'commit'):
        try:
            if argv[0] == 'record':
                if len(argv) < 2:
                    raise JournalError('missing command name')
                record(argv[1], argv[3:] if argv[2:3] == ['--'] else argv[2:])
            else:
                if len(argv) < 3:
                    raise JournalError('missing entry id or exit code')
                commit(argv[1], int(argv[2]))
            return 0
        except Exception as exc:
            kind = 'not journaled' if argv[0] == 'record' else 'commit failed'
            print('undo: {} ({})'.format(kind, exc), file=sys.stderr)
            if os.environ.get('UNDO_DEBUG'):
                traceback.print_exc()
            return 2
    if argv and argv[0] == 'rm':
        try:
            return trash_rm(argv[2:] if argv[1:2] == ['--'] else argv[1:])
        except Exception as exc:
            print('undo: rm failed ({}); not retrying the command'.format(exc), file=sys.stderr)
            if os.environ.get('UNDO_DEBUG'):
                traceback.print_exc()
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('id', nargs='?')
    parser.add_argument('-y', '--yes', action='store_true')
    parser.add_argument('-n', '--dry-run', action='store_true')
    parser.add_argument('-l', '--list', nargs='?', const=DEFAULT_LIST, type=int)
    parser.add_argument('--purge', action='store_true')
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--doctor', action='store_true')
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--version', action='version', version='undo {}'.format(VERSION))
    args = parser.parse_args(argv)
    if args.list is not None and args.list < 0:
        parser.error('list count must be nonnegative')
    try:
        return undo(args)
    except JournalCorrupt as exc:
        print('undo: {}'.format(exc), file=sys.stderr)
        print("undo: run 'undo --repair' to preserve the bad file and salvage valid entries", file=sys.stderr)
        return 2
    except ConflictError as exc:
        print('undo: {}'.format(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print('undo: error: {}'.format(exc), file=sys.stderr)
        if os.environ.get('UNDO_DEBUG'):
            traceback.print_exc()
        return 2


if __name__ == '__main__':
    sys.exit(main())
