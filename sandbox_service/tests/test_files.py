"""The sidecar's file bridge: inputs in, declared outputs back.

Each run gets its own ephemeral cwd: inputs are written there before the
child starts, and after it exits only the *declared* names are read back —
regular files only, each under the byte cap. Anything else is named in
`unsaved` and deleted with the directory. Names are bare file names; a
separator, `..` or a leading dot is refused before anything runs.
"""
from __future__ import annotations

import os
import sys
import unittest

_SVC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SVC_DIR not in sys.path:
    sys.path.insert(0, _SVC_DIR)

import executor  # noqa: E402


class FileBridgeTests(unittest.TestCase):
    def test_inputs_are_readable_and_outputs_come_back(self):
        env = executor.execute(
            "with open('in.txt') as f:\n"
            "    data = f.read()\n"
            "with open('out.txt', 'w') as f:\n"
            "    f.write(data.upper())\n"
            "result = 'done'",
            files={'in.txt': b'hello'},
            collect=['out.txt'],
        )
        self.assertTrue(env["success"], env)
        self.assertEqual(env["files_out"]["out.txt"], b'HELLO')
        self.assertEqual(env["missing"], [])
        self.assertEqual(env["unsaved"], [])

    def test_a_declared_output_never_written_is_missing(self):
        env = executor.execute("result = 1", collect=['nope.txt'])
        self.assertTrue(env["success"], env)
        self.assertEqual(env["missing"], ['nope.txt'])
        self.assertNotIn('nope.txt', env.get("files_out", {}))

    def test_an_undeclared_file_is_unsaved_not_collected(self):
        env = executor.execute(
            "with open('extra.log', 'w') as f:\n    f.write('x')",
            collect=['out.txt'],
        )
        self.assertTrue(env["success"], env)
        self.assertIn('extra.log', env["unsaved"])
        self.assertNotIn('extra.log', env.get("files_out", {}))

    def test_a_symlink_output_is_refused_not_followed(self):
        if os.name != "posix":
            self.skipTest("symlinks need POSIX")
        env = executor.execute(
            "import os\nos.symlink('/etc/hostname', 'link.txt')\nresult = 1",
            collect=['link.txt'],
        )
        self.assertTrue(env["success"], env)
        self.assertIn('link.txt', env["missing"])
        self.assertNotIn('link.txt', env.get("files_out", {}))

    def test_bad_names_are_refused_before_running(self):
        for bad in ('../x', 'a/b', '.hidden', '', 'x' * 129):
            with self.subTest(name=bad):
                with self.assertRaises(executor.FileSpecError):
                    executor.execute("result = 1", files={bad: b'x'})
                with self.assertRaises(executor.FileSpecError):
                    executor.execute("result = 1", collect=[bad])

    def test_too_many_files_are_refused(self):
        many = {f'f{i}.txt': b'x' for i in range(executor.MAX_FILES + 1)}
        with self.assertRaises(executor.FileSpecError):
            executor.execute("result = 1", files=many)

    def test_an_oversized_input_is_refused(self):
        with self.assertRaises(executor.FileSpecError):
            executor.execute(
                "result = 1",
                files={'big.bin': b'x' * (executor.MAX_FILE_BYTES + 1)},
            )

    def test_binary_round_trips(self):
        blob = bytes(range(256)) * 100
        env = executor.execute(
            "with open('in.bin', 'rb') as f:\n"
            "    data = f.read()\n"
            "with open('out.bin', 'wb') as f:\n"
            "    f.write(data)\n"
            "result = len(data)",
            files={'in.bin': blob},
            collect=['out.bin'],
        )
        self.assertTrue(env["success"], env)
        self.assertEqual(env["result"], len(blob))
        self.assertEqual(env["files_out"]["out.bin"], blob)


if __name__ == "__main__":
    unittest.main()
