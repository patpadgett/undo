#!/usr/bin/env python3
"""Best-effort shell journal of structured inverse operations (stdlib only)."""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path


class NotJournaled(Exception):
    pass


def home():
    return Path(os.environ.get('UNDO_HOME', os.path.expanduser('~/.undo'))).absolute()


def exists(path):
    return os.path.lexists(path)


def absolute(path):
    return os.path.abspath(path)


def load():
    try:
        with (home() / 'journal.jsonl').open() as stream:
            return [json.loads(line) for line in stream if line.strip()]
    except FileNotFoundError:
        return []


def save(entries):
    home().mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = home() / ('journal.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w') as stream:
            for entry in entries:
                stream.write(json.dumps(entry) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(home() / 'journal.jsonl'))
    finally:
        if temp.exists():
            temp.unlink()


def purge():
    entries = load()
    cutoff = time.time() - 7 * 86400
    kept = []
    for entry in entries:
        if entry['ts'] < cutoff:
            shutil.rmtree(str(home() / 'trash' / entry['id']), ignore_errors=True)
        else:
            kept.append(entry)
    if len(entries) != len(kept):
        save(kept)


def parse_args(cmd, argv):
    operands, flags, target = [], set(), None
    i, options = 0, True
    short = {'mv': 'fivnT', 'cp': 'fivnTrRpP', 'rm': 'frRv',
             'chmod': 'Rvcf', 'chown': 'Rvcfh', 'mkdir': 'pv',
             'rmdir': 'v', 'ln': 'sivn'}[cmd]
    longs = {'--recursive': 'R', '--force': 'f', '--verbose': 'v',
             '--interactive': 'i', '--no-clobber': 'n', '--parents': 'p',
             '--symbolic': 's', '--no-target-directory': 'T'}
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
                cwd=os.getcwd(), inverse=[], trash=None, status='pending')


def stash(entry, path):
    dest = home() / 'trash' / entry['id'] / path.lstrip(os.sep)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Copy, not rename: preserve the real command's -i/-n and failure semantics.
    if os.path.islink(path):
        os.symlink(os.readlink(path), str(dest))
    elif os.path.isdir(path):
        shutil.copytree(path, str(dest), symlinks=True)
    elif stat.S_ISREG(os.lstat(path).st_mode):
        shutil.copy2(path, str(dest))
    else:
        raise NotJournaled('cannot back up special file')
    entry['trash'] = str(home() / 'trash' / entry['id'])
    return str(dest)


def affected(paths, recursive):
    result = []
    seen = set()
    for path in paths:
        candidates = [path]
        if recursive and not os.path.islink(path) and os.path.isdir(path):
            for root, dirs, files in os.walk(path, followlinks=False):
                candidates.extend(os.path.join(root, name) for name in dirs + files)
                if len(candidates) > 10000:
                    raise NotJournaled('too many files')
        for item in candidates:
            if item not in seen:
                seen.add(item)
                result.append(item)
                if len(result) > 10000:
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
        ops = [['mkdir', absolute(p)] for p in args]
    entry['inverse'] = ops
    return ops


def record(cmd, argv):
    purge()
    entry = new_entry(cmd, argv)
    try:
        compute_inverse(entry)
        entries = load()
        entries.append(entry)
        save(entries)
    except Exception:
        shutil.rmtree(str(home() / 'trash' / entry['id']), ignore_errors=True)
        raise
    print(entry['id'])


def commit(ident, code):
    entries = load()
    entry = next(e for e in entries if e['id'] == ident)
    entry['status'] = 'done' if code == 0 else 'failed'
    # An interactive/no-clobber move may return success without moving anything.
    if code == 0 and entry['cmd'] == 'mv':
        filtered, skip_restore = [], False
        for op in entry['inverse']:
            if op[0] == 'mv':
                skip_restore = exists(op[2])
                if not skip_restore:
                    filtered.append(op)
            elif not skip_restore:
                filtered.append(op)
        entry['inverse'] = filtered
    save(entries)


def trash_rm(argv):
    try:
        paths, flags, _ = parse_args('rm', argv)
    except NotJournaled as exc:
        print('undo: not journaled ({})'.format(exc), file=sys.stderr)
        return subprocess.call(['rm'] + argv)
    if not paths:
        if 'f' in flags:
            return 0
        print('rm: missing operand', file=sys.stderr)
        return 1
    try:
        purge()
        entry = new_entry('rm', argv)
        entries = load()
        entries.append(entry)
        save(entries)
    except Exception as exc:
        # No operand has been touched: a real-rm fallback is safe here.
        print('undo: not journaled ({})'.format(exc), file=sys.stderr)
        return subprocess.call(['rm'] + argv)
    code = 0
    for operand in paths:
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
        if path == '/' or path == os.getcwd() or str(home()) == path or str(home()).startswith(path + os.sep):
            print("undo: refusing unsafe trash path '{}'".format(path), file=sys.stderr)
            code = 1
            continue
        dest = home() / 'trash' / entry['id'] / path.lstrip(os.sep)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if exists(str(dest)):
                raise OSError('duplicate trash destination')
            entry['trash'] = str(home() / 'trash' / entry['id'])
            entry['inverse'].insert(0, ['restore', str(dest), path])
            save(entries)  # Persist inverse before touching the operand.
            shutil.move(path, str(dest))
            if 'v' in flags:
                print("removed '{}'".format(operand))
        except OSError as exc:
            print('rm: {}: {}'.format(operand, exc), file=sys.stderr)
            code = 1
    entry['status'] = 'done' if code == 0 else 'failed'
    save(entries)
    return code


def run_op(op):
    kind = op[0]
    if kind in ('mv', 'restore'):
        src, dst = op[1:]
        if exists(dst):
            raise FileExistsError('target now exists: ' + dst)
        if not exists(src):
            raise FileNotFoundError(src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
    elif kind == 'rm':
        path = op[1]
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    elif kind == 'unlink':
        os.unlink(op[1])
    elif kind == 'rmdir':
        os.rmdir(op[1])
    elif kind == 'mkdir':
        os.makedirs(op[1], exist_ok=False)
    elif kind == 'chmod':
        if os.path.islink(op[2]):
            raise OSError('refusing to chmod a replacement symlink')
        os.chmod(op[2], int(op[1], 8))
    elif kind == 'chown':
        uid, gid = map(int, op[1].split(':'))
        os.lchown(op[2], uid, gid)
    else:
        raise ValueError('unknown inverse ' + kind)


def describe(entry):
    import shlex
    print('{}: {} (cwd {})'.format(entry['id'], shlex.join([entry['cmd']] + entry['argv']), entry['cwd']))
    words = {'mv': 'move', 'restore': 'restore from trash', 'rm': 'remove copy',
             'unlink': 'unlink', 'rmdir': 'remove empty directory', 'mkdir': 'recreate directory',
             'chmod': 'restore mode', 'chown': 'restore owner'}
    for op in entry['inverse']:
        print('  {}: {}'.format(words[op[0]], ' -> '.join(repr(p) for p in op[1:])))


def undo(args):
    if args.purge:
        purge()
        return 0
    entries = load()
    if args.list is not None:
        import shlex
        for entry in entries[-args.list:] if args.list else []:
            age = max(0, int(time.time() - entry['ts']))
            print('{} {}s {} {}'.format(entry['id'], age, entry['status'], shlex.join([entry['cmd']] + entry['argv'])))
        return 0
    candidates = [e for e in entries if e['status'] == 'done' and (not args.id or e['id'] == args.id)]
    if not candidates:
        print('undo: no matching done entry', file=sys.stderr)
        return 1
    entry = candidates[-1]
    describe(entry)
    if args.dry_run:
        return 0
    if not args.yes and input('undo? [y/N] ').strip().lower() not in ('y', 'yes'):
        return 0
    for op in entry['inverse']:
        try:
            run_op(op)
        except Exception as exc:
            entry['status'] = 'failed'
            save(entries)
            print('undo: inverse failed {}: {}'.format(op, exc), file=sys.stderr)
            return 1
    entry['status'] = 'undone'
    save(entries)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == 'record':
            record(argv[1], argv[3:] if argv[2:3] == ['--'] else argv[2:])
            return 0
        if argv and argv[0] == 'commit':
            commit(argv[1], int(argv[2]))
            return 0
        if argv and argv[0] == 'rm':
            return trash_rm(argv[2:] if argv[1:2] == ['--'] else argv[1:])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('id', nargs='?')
        parser.add_argument('-y', '--yes', action='store_true')
        parser.add_argument('-n', '--dry-run', action='store_true')
        parser.add_argument('-l', '--list', nargs='?', const=10, type=int)
        parser.add_argument('--purge', action='store_true')
        args = parser.parse_args(argv)
        if args.list is not None and args.list < 0:
            parser.error('list count must be nonnegative')
        return undo(args)
    except Exception as exc:
        print('undo: not journaled ({})'.format(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
