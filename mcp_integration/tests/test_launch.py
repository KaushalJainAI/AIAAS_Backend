"""`npx -y <pkg>` is rewritten to a direct `node` launch where the image has it.

Two Node processes per connector, of which one — the `npx` launcher — stays
resident for the life of the session doing nothing but holding a pipe. On a
384 MB container with a 150 MB connector budget that is roughly half the
ceiling spent on launchers.

The catalogue row keeps saying `npx -y <pkg>`, because that is the spelling
that works on a machine with nothing installed. The rewrite is a deployment
detail, and it only ever fires when the package is provably present.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase

from mcp_integration import launch


class ResolveLaunchTests(SimpleTestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        patcher = patch.object(launch, "PACKAGE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        launch.clear_cache()
        self.addCleanup(launch.clear_cache)
        which = patch.object(launch.shutil, "which", return_value="/usr/bin/node")
        which.start()
        self.addCleanup(which.stop)

    def _install(self, package: str, bin_field, script: str = "dist/index.js") -> str:
        pkg_dir = os.path.join(self.root, *package.split("/"))
        os.makedirs(os.path.join(pkg_dir, os.path.dirname(script)), exist_ok=True)
        path = os.path.join(pkg_dir, script)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("// server")
        with open(os.path.join(pkg_dir, "package.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": package, "bin": bin_field}, fh)
        # `resolve_launch` normalises what it returns, and on Windows that
        # rewrites the separator the manifest used.
        return os.path.normpath(path)

    def test_an_installed_package_launches_directly(self):
        script = self._install("@modelcontextprotocol/server-memory", "dist/index.js")
        command, args = launch.resolve_launch("npx", ["-y", "@modelcontextprotocol/server-memory"])
        self.assertEqual(command, "/usr/bin/node")
        self.assertEqual(args, [script])

    def test_the_server_keeps_its_own_arguments(self):
        """The filesystem connector takes its allowed directories as argv."""
        script = self._install("@modelcontextprotocol/server-filesystem", "dist/index.js")
        command, args = launch.resolve_launch(
            "npx", ["-y", "@modelcontextprotocol/server-filesystem", "/data", "/tmp"],
        )
        self.assertEqual(command, "/usr/bin/node")
        self.assertEqual(args, [script, "/data", "/tmp"])

    def test_a_bin_map_is_resolved_by_the_package_short_name(self):
        self._install("@scope/gmail-mcp", {"gmail-mcp": "bin/cli.js", "other": "bin/x.js"},
                      script="bin/cli.js")
        command, args = launch.resolve_launch("npx", ["-y", "@scope/gmail-mcp"])
        self.assertEqual(command, "/usr/bin/node")
        self.assertTrue(args[0].endswith(os.path.join("bin", "cli.js")))

    def test_an_ambiguous_bin_map_is_left_to_npx(self):
        """Picking the first key would silently start a different program."""
        self._install("@scope/thing", {"a": "bin/a.js", "b": "bin/b.js"}, script="bin/a.js")
        self.assertEqual(
            launch.resolve_launch("npx", ["-y", "@scope/thing"]),
            ("npx", ["-y", "@scope/thing"]),
        )

    def test_a_package_that_is_not_installed_is_left_alone(self):
        self.assertEqual(
            launch.resolve_launch("npx", ["-y", "@scope/missing"]),
            ("npx", ["-y", "@scope/missing"]),
        )

    def test_a_version_pin_is_left_alone(self):
        """The image holds what npm resolved, which need not be that build."""
        self._install("@scope/thing", "dist/index.js")
        self.assertEqual(
            launch.resolve_launch("npx", ["-y", "@scope/thing@1.2.3"]),
            ("npx", ["-y", "@scope/thing@1.2.3"]),
        )

    def test_non_npx_commands_are_untouched(self):
        self.assertEqual(
            launch.resolve_launch("uvx", ["some-server"]), ("uvx", ["some-server"]),
        )
        self.assertEqual(
            launch.resolve_launch("/usr/bin/python", ["-m", "srv"]),
            ("/usr/bin/python", ["-m", "srv"]),
        )

    def test_a_bin_escaping_the_package_root_is_refused(self):
        """A `bin` of "../../.." must not turn a catalogue row into a path anywhere."""
        pkg_dir = os.path.join(self.root, "evil")
        os.makedirs(pkg_dir, exist_ok=True)
        with open(os.path.join(pkg_dir, "package.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": "evil", "bin": "../../../../bin/sh"}, fh)
        self.assertEqual(
            launch.resolve_launch("npx", ["-y", "evil"]), ("npx", ["-y", "evil"]),
        )

    def test_no_package_root_disables_the_rewrite(self):
        """A developer with no image gets exactly the old behaviour."""
        with patch.object(launch, "PACKAGE_ROOT", ""):
            launch.clear_cache()
            self.assertEqual(
                launch.resolve_launch("npx", ["-y", "@scope/thing"]),
                ("npx", ["-y", "@scope/thing"]),
            )
