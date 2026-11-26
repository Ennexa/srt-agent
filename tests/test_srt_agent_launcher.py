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
import time
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

    def test_policy_always_lists_both_domain_lists(self):
        # srt 0.0.81 refuses a settings file without network.deniedDomains.
        (self.config / "config.jsonc").write_text(json.dumps({"network": {}}))
        network = self.show()["policy"]["network"]
        self.assertEqual((network["allowedDomains"], network["deniedDomains"]), ([], []))

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

    def test_agent_passthrough_skips_the_host_env(self):
        (self.config / "config.jsonc").write_text(json.dumps({
            "network": {"allowedDomains": ["example.com"]}, "passEnv": ["FAKE_PROVIDER_KEY"]}))
        os.environ["FAKE_PROVIDER_KEY"] = "secret"
        os.environ.setdefault("TERM", "xterm-256color")
        self.addCleanup(os.environ.pop, "FAKE_PROVIDER_KEY", None)
        child_env, env = self.launch()[3:5]
        self.assertNotIn("FAKE_PROVIDER_KEY", env)
        self.assertEqual(env["TERM"], os.environ["TERM"])
        agent_env = json.loads(env["SRT_AGENT_CHILD_ENV"])
        self.assertEqual(agent_env["FAKE_PROVIDER_KEY"], "secret")
        self.assertNotIn("FAKE_PROVIDER_KEY", child_env)  # --show prints child_env

    def test_project_path_must_still_resolve_to_the_approved_target(self):
        tools = self.base / "tools"
        (tools / "v1").mkdir(parents=True)
        (tools / "v2").mkdir()
        link = tools / "current"
        link.symlink_to(tools / "v1")
        self.write_project({"path": [str(link)]})
        approved = self.sa.project_permissions(self.project, self.config / "config.jsonc")
        self.assertEqual(approved[1]["launcher"]["resolved_path"], [str(tools / "v1")])
        child_env = self.launch()[3]  # unchanged target: fine, and only the target is used
        self.assertIn(str(tools / "v1"), child_env["PATH"].split(":"))
        self.assertNotIn(str(link), child_env["PATH"].split(":"))
        link.unlink()
        link.symlink_to(tools / "v2")  # retargeted after approval
        original = self.sa.project_permissions
        self.sa.project_permissions = lambda *args, **kwargs: approved
        try:
            with self.assertRaisesRegex(RuntimeError, "not the approved"):
                self.launch()
        finally:
            self.sa.project_permissions = original

    def test_oversized_file_at_a_template_path_is_backed_up_unread(self):
        child_env = self.launch()[3]
        target = Path(child_env["HOME"]) / ".config/x.txt"
        target.unlink()
        with open(target, "wb") as handle:
            handle.truncate(self.sa.TEMPLATE_FILE_LIMIT * 64)  # sparse: big, but cheap to create
        self.launch()
        self.assertEqual(target.read_text(), "hello")
        self.assertEqual(len(list(target.parent.glob("x.txt.srt-agent-backup-*"))), 1)

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


class ApprovalPromptTest(unittest.TestCase):
    """The approval summary and its steps, without a terminal."""

    write_project = LauncherTest.write_project

    def setUp(self):
        LauncherTest.setUp(self)  # same scratch home, without rerunning its tests
        (self.home / "notes").mkdir()
        (self.config / "config.jsonc").write_text(json.dumps({
            "network": {"allowedDomains": ["example.com"]},
            "groups": {
                "docker": {"network": {"allowAllUnixSockets": True,
                                       "allowUnixSockets": ["~/.docker/run/docker.sock"]}},
                "lang/node": {"network": {"allowedDomains": ["registry.npmjs.org"]}},
            }}))

    def state(self, project_data):
        self.write_project(project_data)
        settings = self.config / "config.jsonc"
        _, grants, data, _ = self.sa.project_permissions(self.project, settings)
        return grants, data, self.sa.load_base_policy(self.project, settings, removals=grants["remove"])

    def changes(self, before, after):
        grants, data, policy = self.state(before)
        previous = {"configuration": data, "base_configuration": policy["configuration"],
                    "summary": self.sa.approval_summary(policy, grants)}
        grants, data, policy = self.state(after)
        return self.sa.approval_changes(self.project, previous, data, grants, policy)

    def find(self, changes, text):
        found = [c for c in changes if text in c["text"]]
        self.assertEqual(len(found), 1, text + " in " + repr(changes))
        return found[0]

    def steps(self, changes, answers):
        replies, output, details = iter(answers), [], []
        accepted = self.sa.approval_steps(changes, lambda: next(replies, ""), output.append,
                                          lambda: details.append(1), False)
        return accepted, "".join(output), len(details)

    def test_changes_are_ranked_by_what_they_grant_and_attributed(self):
        changes = self.changes({"filesystem": {"allowWrite": ["./out"]}},
                               {"extends": ["docker", "lang/node"], "setEnv": {"FOO": "1"},
                                "filesystem": {"allowRead": ["~/notes"]}})
        switch = self.find(changes, "allowAllUnixSockets")
        self.assertEqual((switch["level"], switch["source"]), ("high", "via group docker"))
        self.assertEqual(self.find(changes, "docker.sock")["level"], "high")
        domain = self.find(changes, "registry.npmjs.org")
        self.assertEqual((domain["level"], domain["source"]), ("medium", "via group lang/node"))
        self.assertEqual(self.find(changes, '"~/notes"')["level"], "medium")
        self.assertEqual(self.find(changes, 'setEnv "FOO"')["level"], "medium")
        self.assertEqual(self.find(changes, '"./out"')["level"], "low")  # removed: tightens
        # Highest risk last, right above the questions.
        levels = [c["level"] for c in changes]
        self.assertEqual(levels, sorted(levels, key=self.sa.RISK_LEVELS.index, reverse=True))
        self.assertEqual(levels[-1], "high")
        text = self.sa.render_approval(self.project, "settings changed", changes, False)
        self.assertIn('Groups: + "docker", + "lang/node"', text)

    def test_write_inside_the_project_is_low_outside_high(self):
        changes = self.changes({}, {"filesystem": {"allowWrite": ["./out"]}})
        self.assertEqual(self.find(changes, '"./out"')["level"], "low")
        # Projects cannot grant writes outside themselves; the global config can.
        level, _ = self.sa.change_risk("allowWrite", True, str(self.home / "notes"), self.project)
        self.assertEqual(level, "high")

    def test_high_risk_needs_yes_spelled_out_each_time(self):
        changes = self.changes({}, {"extends": ["docker", "lang/node"]})
        self.assertFalse(self.steps(changes, ["yes", "y"])[0])  # second high change: y is not enough
        accepted, output, _ = self.steps(changes, ["yes", "yes", "y"])
        self.assertTrue(accepted)
        self.assertIn("HIGH-RISK change 2 of 2", output)
        self.assertIn("Approve the other changes", output)

    def test_only_high_risk_changes_skip_the_final_step(self):
        changes = self.changes({"extends": ["lang/node"]}, {"extends": ["lang/node", "docker"]})
        changes = [c for c in changes if c.get("kind") != "group"]
        accepted, output, _ = self.steps(changes, ["yes", "yes"])
        self.assertTrue(accepted)
        self.assertNotIn("[y/N/d]", output)

    def test_low_and_medium_changes_take_one_y(self):
        changes = self.changes({}, {"extends": ["lang/node"]})
        accepted, output, _ = self.steps(changes, ["y"])
        self.assertTrue(accepted)
        self.assertNotIn("HIGH-RISK", output)

    def test_d_shows_details_and_asks_again_and_eof_cancels(self):
        changes = self.changes({}, {"extends": ["lang/node"]})
        accepted, output, details = self.steps(changes, ["d", "y"])
        self.assertEqual((accepted, details, output.count("[y/N/d]")), (True, 1, 2))
        self.assertFalse(self.steps(changes, [])[0])

    def test_values_are_escaped(self):
        changes = self.changes({}, {"setEnv": {"FOO": "\x1b[2J\x1b[31mfake"}})
        text = self.sa.render_approval(self.project, "settings changed", changes, False)
        self.assertNotIn("\x1b", text)
        self.assertIn("\\u001b", text)

    def test_first_approval_shows_removed_inherited_blocks_by_their_effect(self):
        (self.home / "secrets").mkdir()
        (self.config / "config.jsonc").write_text(json.dumps({
            "network": {"allowedDomains": ["example.com"], "deniedDomains": ["evil.example"]},
            "filesystem": {"denyWrite": ["~/secrets"]}}))
        self.write_project({"remove": {"filesystem": {"denyWrite": ["~/secrets"]},
                                       "network": {"deniedDomains": ["evil.example"]}}})
        settings = self.config / "config.jsonc"
        _, grants, data, _ = self.sa.project_permissions(self.project, settings)
        policy = self.sa.load_base_policy(self.project, settings, removals=grants["remove"])
        changes = self.sa.approval_changes(self.project, {}, data, grants, policy)
        block = self.find(changes, "denyWrite")
        self.assertEqual((block["level"], block["sign"], block["source"]), ("high", "-", "project remove"))
        self.assertEqual(self.find(changes, "evil.example")["level"], "medium")
        self.assertFalse([c for c in changes if "remove" in c["text"]])  # shown by effect, not as remove.*

    def test_removing_a_grant_launch_checks_refuse_does_not_block_the_prompt(self):
        # A whole-home read needs --allow-home; removed by the project, the
        # launch is fine, and so must be the prompt that describes it.
        (self.config / "config.jsonc").write_text(json.dumps({
            "network": {"allowedDomains": ["example.com"]}, "filesystem": {"allowRead": ["~"]}}))
        self.write_project({"remove": {"filesystem": {"allowRead": ["~"]}}})
        settings = self.config / "config.jsonc"
        _, grants, data, _ = self.sa.project_permissions(self.project, settings)
        policy = self.sa.load_base_policy(self.project, settings, removals=grants["remove"])
        changes = self.sa.approval_changes(self.project, {}, data, grants, policy)
        home = self.find(changes, "allowRead")
        self.assertEqual((home["level"], home["sign"], home["source"]), ("low", "-", "project remove"))

    def test_effective_home_mode_is_compared(self):
        config = json.loads((self.config / "config.jsonc").read_text())
        config["agentHome"] = "shared"
        (self.config / "config.jsonc").write_text(json.dumps(config))
        changes = self.changes({"agentHome": "isolated"}, {})
        home = self.find(changes, "agentHome")
        self.assertEqual(home["text"], 'agentHome: "isolated" → "shared"')
        self.assertEqual((home["level"], home["source"]), ("medium", "global config"))
        self.assertIn("credentials", home["note"])
        self.assertEqual(self.find(self.changes({}, {"agentHome": "isolated"}), "agentHome")["level"], "low")

    def test_setenv_is_credited_to_the_layer_that_wins(self):
        config = json.loads((self.config / "config.jsonc").read_text())
        config["groups"]["tools"] = {"setEnv": {"TOOL_MODE": "fast"}}
        config["groups"]["tools-late"] = {"setEnv": {"TOOL_MODE": "late"}}
        (self.config / "config.jsonc").write_text(json.dumps(config))
        own = self.find(self.changes({}, {"extends": ["tools"], "setEnv": {"TOOL_MODE": "slow"}}), "TOOL_MODE")
        self.assertEqual((own["text"], own["source"]), ('setEnv "TOOL_MODE": "slow"', None))
        later = self.find(self.changes({}, {"extends": ["tools", "tools-late"]}), "TOOL_MODE")
        self.assertEqual((later["text"], later["source"]), ('setEnv "TOOL_MODE": "late"', "via group tools-late"))

    def test_group_launcher_settings_are_shown_in_order(self):
        (self.home / "tools/a").mkdir(parents=True)
        (self.home / "tools/b").mkdir(parents=True)
        config = json.loads((self.config / "config.jsonc").read_text())
        config["groups"]["tools"] = {"path": ["~/tools/a"], "setEnv": {"TOOL_MODE": "fast"}}
        (self.config / "config.jsonc").write_text(json.dumps(config))
        changes = self.changes({}, {"extends": ["tools"]})
        path = self.find(changes, 'path "~/tools/a"')
        self.assertEqual((path["level"], path["source"]), ("medium", "via group tools"))
        self.assertEqual(self.find(changes, 'setEnv "TOOL_MODE"')["source"], "via group tools")
        reordered = self.changes({"path": ["~/tools/a", "~/tools/b"]}, {"path": ["~/tools/b", "~/tools/a"]})
        self.assertEqual(self.find(reordered, "path order")["level"], "medium")

    def test_ordered_settings_keep_their_order(self):
        flat = self.sa.flat_settings
        self.assertNotEqual(flat({"notify": {"command": ["/a", "x", "y"]}}),
                            flat({"notify": {"command": ["/a", "y", "x"]}}))
        self.assertEqual(flat({"passEnv": ["A", "B"]}), flat({"passEnv": ["B", "A"]}))

    def test_unchanged_effect_still_shows_something(self):
        grants, data, policy = self.state({})
        previous = {"configuration": data, "base_configuration": policy["configuration"],
                    "summary": self.sa.approval_summary(policy, grants)}
        changes = self.sa.approval_changes(self.project, previous, data, grants, policy)
        self.assertEqual([c["text"] for c in changes], ["resolved paths or bound files changed"])


class NotifyRelayTest(unittest.TestCase):
    """Drives a real forked relay through its FIFO, with short limits."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.sa = load_launcher()
        home = self.base / "home"
        home.mkdir()
        self.sa.account_home = lambda: home
        self.log = self.sa.approval_directory(create=True).parent / "notify.log"
        self.temp = self.base / "tmp"
        self.temp.mkdir()
        self.sa.NOTIFY_MIN_INTERVAL = 0
        self.sa.NOTIFY_LOG_SUMMARY = 0

    def start(self, script):
        program = self.base / "notify.sh"
        program.write_text("#!/bin/sh\n" + script)
        program.chmod(0o755)
        fifo = self.sa.start_notify_relay([str(program)], str(self.temp), "claude", self.base)
        writer = os.open(fifo, os.O_WRONLY)
        self.addCleanup(os.close, writer)
        return writer

    def wait_for(self, text, count=1, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            log = self.log.read_text() if self.log.exists() else ""
            if log.count(text) >= count:
                return log
            time.sleep(0.1)
        self.fail("log never showed " + repr(text) + " x" + str(count) + ":\n" + log)

    def test_junk_lines_are_summarized_not_logged_one_by_one(self):
        self.sa.NOTIFY_LOG_SUMMARY = 1
        writer = self.start("exit 0\n")
        os.write(writer, b"junk\n" * 500 + b"stop\n")
        log = self.wait_for("event stop: exit 0")
        self.wait_for("lines that are not event words")
        self.assertLess(log.count("\n"), 50)

    def test_a_new_event_beyond_the_cap_replaces_the_oldest(self):
        self.sa.NOTIFY_KILL_GRACE = 1
        writer = self.start("sleep 30\n")
        words = list(self.sa.NOTIFY_EVENTS)[:5]
        os.write(writer, b"".join(w.encode() + b"\n" for w in words))
        log = self.wait_for(": started", count=5)
        self.wait_for("older notifications replaced by newer ones")
        self.assertIn("event " + words[0] + ": exit", log + self.log.read_text())

    def test_stderr_capture_is_bounded_on_disk(self):
        self.sa.NOTIFY_MAX_FILE = 64 * 1024
        writer = self.start("head -c 10000000 /dev/zero >&2\necho done >&2\n")
        os.write(writer, b"stop\n")
        log = self.wait_for("event stop: exit")
        line = [l for l in log.splitlines() if "event stop: exit" in l][0]
        self.assertLess(len(line), 2300)  # tail only
        self.assertNotIn("done", line)  # writing stopped at the limit (unbounded, "done" would be the tail)

    def test_helpers_left_behind_count_until_they_end(self):
        self.sa.NOTIFY_MAX_SECONDS = 1
        self.sa.NOTIFY_KILL_GRACE = 1
        writer = self.start("sleep 30 &\nexit 0\n")  # like the click helper: same group, outlives the program
        os.write(writer, b"stop\n")
        self.wait_for("event stop: exit 0")
        self.wait_for("event stop: stopped after time limit")

    def test_a_program_ignoring_sigterm_is_killed(self):
        self.sa.NOTIFY_MAX_SECONDS = 0.5
        self.sa.NOTIFY_KILL_GRACE = 1
        writer = self.start("trap '' TERM\nwhile :; do sleep 1; done\n")
        os.write(writer, b"stop\n")
        self.wait_for("stopped after time limit")


if __name__ == "__main__":
    unittest.main()
