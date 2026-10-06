import contextlib
import importlib.util
import io
import os
from pathlib import Path
import stat
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
        entry = self.entry('mv', ['-t', 'out', '--', '-file'])
        self.assertEqual(entry['inverse'][0], ['mv', str(self.root / 'out/-file'), str(self.root / '-file')])

    def test_recursive_cap(self):
        with patch.object(undo.os, 'walk', return_value=[('root', [], [str(i) for i in range(10001)])]), patch.object(undo.os.path, 'isdir', return_value=True):
            with self.assertRaisesRegex(undo.NotJournaled, 'too many files'):
                undo.affected(['root'], True)

    def test_purge(self):
        entry = undo.new_entry('mkdir', ['a'])
        entry['ts'] = 0
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


if __name__ == '__main__':
    unittest.main()
