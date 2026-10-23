import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('undo', Path(__file__).resolve().parents[1] / 'undo.py')
assert spec is not None and spec.loader is not None
undo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(undo)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'UNDO_HOME': str(self.root / 'journal')})
        self.env.start()
        self.oldcwd = os.getcwd()
        os.chdir(str(self.root))

    def tearDown(self):
        os.chdir(self.oldcwd)
        self.env.stop()
        self.temp.cleanup()

    def entry(self, cmd, args):
        entry = undo.new_entry(cmd, args)
        undo.compute_inverse(entry)
        return entry

    def test_mv_inverse(self):
        Path('a').write_text('new')
        entry = self.entry('mv', ['a', 'b'])
        self.assertEqual(entry['inverse'], [['mv', str(self.root / 'b'), str(self.root / 'a')]])

    def test_mv_backup_preserves_destination_before_command(self):
        Path('a').write_text('new')
        Path('b').write_text('old')
        entry = self.entry('mv', ['a', 'b'])
        self.assertEqual(Path('b').read_text(), 'old')
        self.assertEqual(entry['inverse'][1][0], 'restore')
        os.replace('a', 'b')
        for op in entry['inverse']:
            undo.run_op(op)
        self.assertEqual(Path('a').read_text(), 'new')
        self.assertEqual(Path('b').read_text(), 'old')

    def test_cp_inverse(self):
        Path('a').write_text('new')
        entry = self.entry('cp', ['a', 'b'])
        self.assertEqual(entry['inverse'], [['rm', str(self.root / 'b')]])

    def test_chmod_inverse(self):
        Path('a').touch()
        os.chmod('a', 0o644)
        entry = self.entry('chmod', ['600', 'a'])
        self.assertEqual(entry['inverse'], [['chmod', '0644', str(self.root / 'a')]])
        os.chmod('a', 0o600)
        undo.run_op(entry['inverse'][0])
        self.assertEqual(stat.S_IMODE(os.stat('a').st_mode), 0o644)

    def test_mkdir_parents_inverse(self):
        Path('existing').mkdir()
        entry = self.entry('mkdir', ['-p', 'existing/a/b'])
        self.assertEqual(entry['inverse'], [['rmdir', str(self.root / 'existing/a/b')], ['rmdir', str(self.root / 'existing/a')]])

    def test_journal_round_trip(self):
        entry = undo.new_entry('mkdir', ['with spaces'])
        undo.save([entry])
        self.assertEqual(undo.load(), [entry])
        undo.commit(entry['id'], 1)
        self.assertEqual(undo.load()[0]['status'], 'failed')

    def test_trash_restore_recreates_parent(self):
        Path('parent').mkdir()
        Path('parent/file').write_text('content')
        self.assertEqual(undo.trash_rm(['parent/file']), 0)
        Path('parent').rmdir()
        entry = undo.load()[-1]
        undo.run_op(entry['inverse'][0])
        self.assertEqual(Path('parent/file').read_text(), 'content')

    def test_restore_refuses_collision(self):
        Path('a').write_text('old')
        undo.trash_rm(['a'])
        Path('a').write_text('new')
        with self.assertRaises(FileExistsError):
            undo.run_op(undo.load()[-1]['inverse'][0])
        self.assertEqual(Path('a').read_text(), 'new')

    def test_symlink_rm_restore(self):
        os.symlink('absent', 'link')
        undo.trash_rm(['link'])
        undo.run_op(undo.load()[-1]['inverse'][0])
        self.assertEqual(os.readlink('link'), 'absent')

    def test_target_directory_and_double_dash(self):
        Path('out').mkdir()
        Path('-file').write_text('x')
        entry = self.entry('mv', ['-t', 'out', '--', '-file'])
        self.assertEqual(entry['inverse'][0], ['mv', str(self.root / 'out/-file'), str(self.root / '-file')])

    def test_recursive_cap(self):
        with patch.object(undo.os, 'walk', return_value=[('root', [], [str(i) for i in range(10001)])]), patch.object(undo.os.path, 'isdir', return_value=True):
            with self.assertRaisesRegex(undo.NotJournaled, 'too many files'):
                undo.affected(['root'], True)

    def test_purge(self):
        entry = undo.new_entry('mkdir', ['a'])
        entry['ts'] = 0
        entry['status'] = 'done'
        trash = undo.home() / 'trash' / entry['id']
        trash.mkdir(parents=True)
        (trash / 'old').touch()
        undo.save([entry])
        undo.purge()
        self.assertEqual(undo.load(), [])
        self.assertFalse(trash.exists())

    def test_dry_run_and_specific_id(self):
        Path('a').write_text('content')
        undo.trash_rm(['a'])
        entry = undo.load()[-1]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(undo.main(['-n', entry['id']]), 0)
            self.assertFalse(Path('a').exists())
            self.assertEqual(undo.main(['-y', entry['id']]), 0)
        self.assertEqual(undo.load()[-1]['status'], 'undone')

    def test_rm_initial_journal_failure_falls_through(self):
        with patch.object(undo, 'save', side_effect=OSError('disk full')), patch.object(undo.subprocess, 'call', return_value=0) as call, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.trash_rm(['file']), 0)
        call.assert_called_once_with(['rm', 'file'])

    def test_rm_initial_journal_failure_does_not_run_unsafe_fallback(self):
        with patch.object(undo, 'save', side_effect=OSError('disk full')), \
                patch.object(undo.subprocess, 'call') as call, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.trash_rm(['-r', '..']), 2)
        call.assert_not_called()

    def test_chown_inverse_numeric_owner(self):
        Path('a').touch()
        info = os.lstat('a')
        entry = self.entry('chown', ['{}:{}'.format(info.st_uid, info.st_gid), 'a'])
        self.assertEqual(entry['inverse'], [['chown', '{}:{}'.format(info.st_uid, info.st_gid), str(self.root / 'a')]])
        with patch.object(undo.os, 'lchown') as call:
            undo.run_op(entry['inverse'][0])
        call.assert_called_once_with(str(self.root / 'a'), info.st_uid, info.st_gid)

    def test_recursive_chmod_restores_tree(self):
        Path('tree').mkdir(mode=0o755)
        Path('tree/file').touch()
        os.chmod('tree/file', 0o644)
        entry = self.entry('chmod', ['-R', '700', 'tree'])
        self.assertEqual(len(entry['inverse']), 2)
        for path in ('tree', 'tree/file'):
            os.chmod(path, 0o700)
        for op in entry['inverse']:
            undo.run_op(op)
        self.assertEqual(stat.S_IMODE(os.stat('tree/file').st_mode), 0o644)

    def test_failed_inverse_marks_failed(self):
        Path('a').write_text('old')
        undo.trash_rm(['a'])
        Path('a').write_text('new')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.main(['-y']), 1)
        self.assertEqual(undo.load()[-1]['status'], 'failed')
        self.assertEqual(Path('a').read_text(), 'new')

    def test_unknown_rm_option_passthrough(self):
        with patch.object(undo.subprocess, 'call', return_value=7) as call, contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(undo.trash_rm(['--unsupported', 'a']), 7)
        call.assert_called_once_with(['rm', '--unsupported', 'a'])
        self.assertIn('not journaled (unknown option --unsupported)', errors.getvalue())


    def test_noop_entry_does_not_mask_previous_entry(self):
        Path('existing').mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            undo.record('mkdir', ['realdir'])
            real_id = undo.load()[-1]['id']
        Path('realdir').mkdir()
        undo.commit(real_id, 0)
        with contextlib.redirect_stdout(io.StringIO()):
            undo.record('mkdir', ['-p', 'existing'])
            noop_id = undo.load()[-1]['id']
        undo.commit(noop_id, 0)
        self.assertEqual(undo.load()[-1]['status'], 'noop')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(undo.main(['-y']), 0)
        self.assertFalse(Path('realdir').exists())
        self.assertTrue(Path('existing').is_dir())

    def test_rm_fallback_rejects_unsafe_options_and_operands(self):
        self.assertFalse(undo._rm_fallback_allowed(['-rf', '--no-preserve-root', 'safe']))
        self.assertFalse(undo._rm_fallback_allowed(['-rf', '--preserve-root=no', 'safe']))
        self.assertFalse(undo._rm_fallback_allowed(['-rf', '..']))
        self.assertFalse(undo._rm_fallback_allowed(['-rf', '/']))
        self.assertFalse(undo._rm_fallback_allowed(['-rf', '//']))
        self.assertTrue(undo._rm_fallback_allowed(['-rf', 'safe-dir']))
        self.assertTrue(undo._rm_fallback_allowed(['--help']))

    def test_rm_refuses_dotdot_operand(self):
        work = Path('work/sub')
        work.mkdir(parents=True)
        oldcwd = os.getcwd()
        os.chdir(str(work))
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = undo.trash_rm(['-r', '..'])
        finally:
            os.chdir(oldcwd)
        self.assertEqual(rc, 1)
        self.assertTrue((self.root / 'work').is_dir())

    def test_rmdir_multiple_inverse_order(self):
        Path('x/y').mkdir(parents=True)
        entry = self.entry('rmdir', ['x/y', 'x'])
        self.assertEqual([op[1] for op in entry['inverse']],
                         [str(self.root / 'x'), str(self.root / 'x/y')])
        Path('x/y').rmdir()
        Path('x').rmdir()
        for op in entry['inverse']:
            undo.run_op(op)
        self.assertTrue(Path('x/y').is_dir())

    def test_corrupt_journal_can_be_repaired(self):
        state = undo.home()
        state.mkdir(parents=True)
        valid = undo.new_entry('mkdir', ['a'])
        undo.journal_path().write_bytes(
            b'{bad}\n\xff\n' + json.dumps(valid).encode('utf-8') + b'\n')
        with self.assertRaises(undo.JournalCorrupt):
            undo.load()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.main(['--repair']), 0)
        self.assertEqual([entry['id'] for entry in undo.load()], [valid['id']])
        self.assertEqual(len(list(state.glob('journal.jsonl.corrupt-*'))), 1)

    def test_state_permissions_are_hardened(self):
        state = undo.home()
        state.mkdir(parents=True)
        os.chmod(state, 0o755)
        entry = undo.new_entry('mkdir', ['a'])
        undo.save([entry])
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(undo.journal_path()).st_mode), 0o600)
        Path('dest').write_text('old')
        stash_entry = undo.new_entry('mv', ['src', 'dest'])
        undo.stash(stash_entry, str(Path('dest').absolute()))
        root = state / 'trash' / stash_entry['id']
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)

    def test_stash_refuses_journal_ancestor(self):
        dest = Path('dest')
        dest.mkdir()
        old_home = os.environ['UNDO_HOME']
        os.environ['UNDO_HOME'] = str(dest / '.undo')
        try:
            entry = undo.new_entry('mv', ['src', 'dest'])
            with self.assertRaises(undo.NotJournaled):
                undo.stash(entry, str(dest.absolute()))
        finally:
            os.environ['UNDO_HOME'] = old_home

    def test_purge_saves_before_deleting_trash(self):
        entry = undo.new_entry('mkdir', ['a'])
        entry['ts'] = 0
        entry['status'] = 'done'
        trash = undo.home() / 'trash' / entry['id']
        trash.mkdir(parents=True)
        undo.save([entry])
        calls = []

        def fake_save(entries, backup=True):
            calls.append('save')

        def fake_rmtree(path, **kwargs):
            calls.append('rmtree')

        with patch.object(undo, 'save', side_effect=fake_save), \
                patch.object(undo.shutil, 'rmtree', side_effect=fake_rmtree):
            undo.purge()
        self.assertEqual(calls[:2], ['save', 'rmtree'])

    def test_partial_undo_can_be_recovered(self):
        Path('a').write_text('A')
        Path('b').write_text('B')
        self.assertEqual(undo.trash_rm(['a', 'b']), 0)
        entry = undo.load()[-1]
        original = undo.run_op
        calls = {'count': 0}

        def flaky(op):
            calls['count'] += 1
            if calls['count'] == 2:
                raise OSError('boom')
            return original(op)

        with patch.object(undo, 'run_op', side_effect=flaky), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.main(['-y', entry['id']]), 1)
        self.assertEqual(undo.load()[-1]['status'], 'failed')
        self.assertEqual(undo.read_progress(entry['id']), 1)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.main(['--recover', entry['id'], '-y']), 0)
        self.assertEqual(Path('a').read_text(), 'A')
        self.assertEqual(Path('b').read_text(), 'B')
        self.assertEqual(undo.load()[-1]['status'], 'undone')

    def test_out_of_order_undo_requires_force(self):
        Path('a').write_text('A')
        Path('b').write_text('B')
        self.assertEqual(undo.trash_rm(['a']), 0)
        first = undo.load()[-1]['id']
        self.assertEqual(undo.trash_rm(['b']), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.main(['-y', first]), 1)
        self.assertFalse(Path('a').exists())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.main(['-y', '--force', first]), 0)
        self.assertTrue(Path('a').exists())

    def test_cp_conflict_requires_force(self):
        Path('src').write_text('new')
        entry = undo.new_entry('cp', ['src', 'dest'])
        undo.compute_inverse(entry)
        undo.save([entry])
        shutil.copy2('src', 'dest')
        undo.commit(entry['id'], 0)
        Path('dest').write_text('changed')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.main(['-y', entry['id']]), 1)
        self.assertEqual(Path('dest').read_text(), 'changed')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(undo.main(['-y', '--force', entry['id']]), 0)
        self.assertFalse(Path('dest').exists())

    def test_common_cp_flags_and_ln_no_clobber(self):
        Path('src').write_text('x')
        for index, flag in enumerate(('-a', '--archive', '-d', '-L', '--no-dereference',
                                      '-l', '--link', '-s', '--symbolic-link')):
            entry = self.entry('cp', [flag, 'src', 'dest-{}'.format(index)])
            self.assertEqual(entry['inverse'][0][0], 'rm')
        with self.assertRaises(undo.NotJournaled):
            self.entry('ln', ['-n', 'src', 'link'])
        with self.assertRaises(undo.NotJournaled):
            self.entry('cp', ['--parents', 'src', 'dest'])

    def test_commit_missing_entry_raises(self):
        with self.assertRaises(undo.JournalError):
            undo.commit('0' * 32, 0)

    def test_concurrent_records_are_serialized(self):
        engine = Path(__file__).resolve().parents[1] / 'undo.py'
        env = os.environ.copy()
        processes = [subprocess.Popen(
            [sys.executable, str(engine), 'record', 'mkdir', '--', 'd'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
            for _ in range(8)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr or stdout)
        self.assertEqual(len(undo.load()), 8)

    def test_journal_backup_keeps_previous_version(self):
        first = undo.new_entry('mkdir', ['a'])
        undo.save([first])
        second = undo.new_entry('mkdir', ['b'])
        undo.save([first, second])
        self.assertEqual([entry['id'] for entry in undo.load(undo.backup_path())], [first['id']])


if __name__ == '__main__':
    unittest.main()
