"""Tests for the srt-agent launcher's policy building, against a scratch
account home with fake srt and claude binaries. Nothing is launched.

Run: make test   (or: /usr/bin/python3 -I tests/test_srt_agent_launcher.py)
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "srt-agent"
COMMAND_LINE_TOOLS = Path("/Library/Developer/CommandLineTools")


def load_launcher():
    loader = importlib.machinery.SourceFileLoader("srt_agent", str(SCRIPT))
    spec = importlib.util.spec_from_loader("srt_agent", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / "home"
        self.config = self.home / ".config/srt-agent"
        (self.config / "home/common/.config").mkdir(parents=True)
        (self.config / "config.jsonc").write_text(json.dumps({"network": {"allowedDomains": ["example.com"]}}))
        (self.config / "home/common/.config/x.txt").write_text("hello")
        bindir = self.base / "bin"
        bindir.mkdir()
        for name in ("srt", "claude"):
            (bindir / name).write_text("#!/bin/sh\n")
            (bindir / name).chmod(0o755)
        self.project = self.base / "proj"
        (self.project / ".git").mkdir(parents=True)
        self.write_project({})
        self.sa = load_launcher()
        self.sa.account_home = lambda: self.home
        self.sa.find_binary = lambda binary, agent, project: str(bindir / binary)
        # Real launches create /tmp aliases; keep tests out of the real /tmp.
        real_short_tmp = self.sa.short_tmp
        self.sa.short_tmp = lambda root, kind, name, target, create=True: real_short_tmp(
            root, kind, name, target, create=False)
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)

    def write_project(self, extra):
        (self.project / ".srt-agent.jsonc").write_text(json.dumps({"filesystem": {}, **extra}))

    def tree(self):
        return sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*"))

    def show(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.sa.main(["run", "--show", "claude"])
        return json.loads(out.getvalue())

    def launch(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.sa.prepare("claude", self.project, self.config / "config.jsonc",
                                   require_project_approval=False)

    def test_show_writes_nothing(self):
        before = self.tree()
        result = self.show()
        self.assertEqual(self.tree(), before)
        self.assertEqual(result["template_files"], [".config/x.txt"])
        self.assertEqual(result["command"][2], "<policy file written at launch>")

    def test_launch_writes_the_policy_show_printed(self):
        shown = self.show()["policy"]
        values = self.launch()
        policy_path, child_env = Path(values[2]), values[3]
        self.assertEqual(json.loads(policy_path.read_text()), shown)
        self.assertEqual((Path(child_env["HOME"]) / ".config/x.txt").read_text(), "hello")

    def test_launch_uses_the_approved_template_snapshot(self):
        policy = self.sa.load_base_policy(self.project, self.config / "config.jsonc")
        (self.config / "home/common/.config/x.txt").write_text("changed after approval")
        original = self.sa.load_base_policy
        self.sa.load_base_policy = lambda *args, **kwargs: policy
        try:
            child_env = self.launch()[3]
        finally:
            self.sa.load_base_policy = original
        self.assertEqual((Path(child_env["HOME"]) / ".config/x.txt").read_text(), "hello")

    def test_snapshots_stay_out_of_the_approval_identity(self):
        policy = self.sa.load_base_policy(self.project, self.config / "config.jsonc")
        self.assertIn("_template_scopes", policy)
        identity = self.sa.project_identity(self.project, self.project / ".srt-agent.jsonc", b"{}",
                                            {}, policy)
        self.assertFalse([k for k in identity["base_policy"] if k.startswith("_")])

    def test_project_cannot_turn_on_host_only_switches(self):
        for layer in ({"allowAppleEvents": True}, {"enableWeakerNestedSandbox": True},
                      {"enableWeakerNetworkIsolation": True}, {"network": {"allowAllUnixSockets": True}}):
            self.write_project(layer)
            with self.subTest(layer=layer), self.assertRaisesRegex(RuntimeError, "only in the global config"):
                self.show()

    def test_project_may_turn_them_off_and_bind_locally(self):
        self.write_project({"allowAppleEvents": False, "network": {"allowLocalBinding": True}})
        policy = self.show()["policy"]
        self.assertTrue(policy["network"]["allowLocalBinding"])
        self.assertFalse(policy["allowAppleEvents"])

    def test_tmp_alias_kinds_never_collide(self):
        alias = lambda kind, name: self.sa.short_tmp(self.project, kind, name, None, create=False)
        self.assertNotEqual(alias("agent", "claude-srt"), alias("host", "claude"))
        self.assertNotEqual(alias("agent", "claude-msg"), alias("messaging", "claude"))

    def test_untrusted_developer_dir_stays_out_of_the_host_env(self):
        fake = self.base / "toolchain"
        fake.mkdir()
        self.write_project({"setEnv": {"DEVELOPER_DIR": str(fake)}})
        child_env, env = self.launch()[3:5]
        self.assertEqual(child_env["DEVELOPER_DIR"], str(fake))
        self.assertNotIn("DEVELOPER_DIR", env)

    @unittest.skipUnless(COMMAND_LINE_TOOLS.is_dir(), "needs the Command Line Tools")
    def test_root_owned_developer_dir_reaches_the_host_env(self):
        self.write_project({"setEnv": {"DEVELOPER_DIR": str(COMMAND_LINE_TOOLS)}})
        env = self.launch()[4]
        self.assertEqual(env["DEVELOPER_DIR"], str(COMMAND_LINE_TOOLS.resolve()))


if __name__ == "__main__":
    unittest.main()
