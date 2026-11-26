"""Integration tests: the policy srt-agent writes, enforced by the real srt
and Seatbelt. Each probe runs a shell command in the sandbox and checks that
required access works and forbidden access fails.

Run on the host, never inside a sandbox (sandboxes do not nest):
    make integration
    SRT_AGENT_INTEGRATION_NETWORK=0 make integration   # skip the network probes
    SRT_AGENT_SRT_VERSION=x.y.z make integration       # also pin the srt version

Uses a scratch account home (config, approvals, agent storage) under $TMPDIR,
never yours. Approval prompts are skipped: tests/test_srt_agent_launcher.py
covers them. The /tmp aliases a launch creates are removed afterwards.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "srt-agent"
AGENT = "probe"
NETWORK = os.environ.get("SRT_AGENT_INTEGRATION_NETWORK", "1") != "0"
STARTED = "srt-agent-probe-started"


class Captured(Exception):
    pass


def load_launcher():
    loader = importlib.machinery.SourceFileLoader("srt_agent", str(SCRIPT))
    spec = importlib.util.spec_from_loader("srt_agent", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt is macOS only")
class SeatbeltTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("SANDBOX_RUNTIME") == "1":
            raise RuntimeError("Run the integration tests on the host: sandboxes do not nest")
        cls.base = Path(tempfile.mkdtemp(prefix="srt-agent-integration-")).resolve()
        cls.home = cls.base / "home"
        cls.config = cls.home / ".config/srt-agent"
        cls.config.mkdir(parents=True)
        cls.settings = cls.config / "config.jsonc"
        developer = os.environ.get("DEVELOPER_DIR") or "/Library/Developer/CommandLineTools"
        if not Path(developer).is_dir():
            raise RuntimeError("Set DEVELOPER_DIR: the in-sandbox wrapper is the /usr/bin/python3 xcrun stub")
        cls.settings.write_text(json.dumps({
            # The README's recommended global config (system reads, DNS), so
            # the probes exercise the setup people actually run.
            "network": {
                "allowedDomains": ["example.com"],
                "allowUnixSockets": ["/private/var/run/mDNSResponder"],
                "allowMachLookup": ["com.apple.trustd.agent", "com.apple.dnssd.service",
                                    "com.apple.SystemConfiguration.DNSConfiguration",
                                    "com.apple.SystemConfiguration.configd"],
            },
            "filesystem": {
                "allowRead": ["/bin", "/sbin", "/usr", "/etc", "/private/etc",
                              "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty", "/dev/fd",
                              "/System/Library", "/System/Cryptexes/OS",
                              "/Library/Developer", "/Library/Frameworks",
                              "/opt/homebrew/bin", "/opt/homebrew/sbin",
                              "/opt/homebrew/opt", "/opt/homebrew/Frameworks"],
            },
            # As the README recommends: the xcrun stubs then need no /var/select.
            "setEnv": {"DEVELOPER_DIR": developer},
            "agents": {AGENT: {"binary": "sh"}},
            "groups": {"data": {"filesystem": {"allowRead": ["~/shared-data"]},
                                "network": {"allowedDomains": ["example.org"]}}},
        }))
        (cls.home / "secret.txt").write_text("account secret\n")
        (cls.home / "shared-data").mkdir()
        (cls.home / "shared-data/file.txt").write_text("shared\n")
        (cls.home / "private").mkdir()
        (cls.home / "private/file.txt").write_text("private\n")
        cls.project = cls.base / "project"
        (cls.project / ".git/hooks").mkdir(parents=True)
        (cls.project / ".git/config").write_text("[core]\n")
        cls.sa = load_launcher()
        cls.sa.account_home = lambda: cls.home
        cls.sa.require_approval = lambda *args, **kwargs: None  # covered by the unit tests
        cls.srt = cls.sa.find_binary("srt", "srt")
        version = subprocess.run([cls.srt, "--version"], capture_output=True, text=True, timeout=60)
        cls.srt_version = (version.stdout or version.stderr).strip()
        print("\nsrt: " + cls.srt + " " + cls.srt_version, file=sys.stderr)
        wanted = os.environ.get("SRT_AGENT_SRT_VERSION")
        if wanted and wanted not in cls.srt_version:
            raise RuntimeError("srt is " + cls.srt_version + ", expected " + wanted)
        cls.cwd = os.getcwd()

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        for kind in ("agent", "host"):
            link = cls.sa.short_tmp(cls.project, kind, AGENT, None, create=False)
            if link.is_symlink() and link.lstat().st_uid == os.getuid():
                link.unlink()
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.write_project({})

    def write_project(self, extra):
        # What srt-agent project init writes: the workspace, readable and writable.
        (self.project / ".srt-agent.jsonc").write_text(json.dumps(
            {"filesystem": {"allowRead": ["."], "allowWrite": ["."]}, **extra}))

    @property
    def storage(self):
        return self.sa.project_storage_path(self.project)

    def run_in_sandbox(self, command):
        # The exact command and environment main() would exec. The marker
        # proves the command started inside the sandbox: a refusal only
        # counts if srt actually ran it.
        command = "echo " + STARTED + "; " + command
        captured = {}

        def execve(path, argv, env):
            captured.update(path=path, argv=argv, env=env)
            raise Captured()

        with mock.patch.object(os, "execve", execve):
            try:
                self.sa.main(["exec", "--settings", str(self.settings), "--project", str(self.project),
                              "--no-pty-isolation", AGENT, "--", "/bin/sh", "-c", command])
            except Captured:
                pass
        self.assertTrue(captured, "the launcher did not reach exec")
        result = subprocess.run(captured["argv"], env=captured["env"], cwd=str(self.project),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertIn(STARTED, result.stdout, "srt did not start the command\nexit: " + str(result.returncode)
                      + "\nstdout: " + result.stdout + "\nstderr: " + result.stderr)
        return result

    def allowed(self, command):
        result = self.run_in_sandbox(command)
        self.assertEqual(result.returncode, 0, "expected to work: " + command + "\nstdout: " + result.stdout
                         + "\nstderr: " + result.stderr)
        return result

    def refused(self, command):
        result = self.run_in_sandbox(command)
        self.assertNotEqual(result.returncode, 0, "expected to be refused: " + command + "\nstdout: "
                            + result.stdout + "\nstderr: " + result.stderr)
        return result

    # Filesystem: what the agent needs.

    def test_the_workspace_is_writable(self):
        self.allowed("echo ok > '" + str(self.project / "out.txt") + "'")
        self.assertEqual((self.project / "out.txt").read_text(), "ok\n")

    def test_without_a_write_grant_the_workspace_is_not_writable(self):
        self.write_project({"filesystem": {"allowRead": ["."]}})
        self.allowed("cat '" + str(self.project / ".srt-agent.jsonc") + "' >/dev/null")
        self.refused("echo ok > '" + str(self.project / "denied.txt") + "'")
        self.assertFalse((self.project / "denied.txt").exists())

    def test_the_agent_home_and_temp_are_writable(self):
        self.allowed('echo ok > "$HOME/note" && echo ok > "$TMPDIR/note"')
        self.assertTrue((self.storage / "agents" / AGENT / "home/note").is_file())

    # Filesystem: what it must not reach.

    def test_the_account_home_is_unreadable(self):
        self.refused("cat '" + str(self.home / "secret.txt") + "'")
        self.refused("ls '" + str(self.home) + "'")

    def test_the_approval_store_is_unreachable(self):
        state = self.home / ".local/state/srt-agent"
        self.allowed("true")  # make sure a launch has created it
        self.assertTrue(state.is_dir())
        self.refused("ls '" + str(state) + "'")
        self.refused("echo x > '" + str(state / "approvals/forged.json") + "'")

    def test_the_host_temp_folder_is_unreachable(self):
        self.allowed("true")
        host_temp = self.storage / "srt-tmp" / AGENT
        self.assertTrue(host_temp.is_dir())
        self.refused("ls '" + str(host_temp) + "'")
        self.refused("echo x > '" + str(host_temp / "planted") + "'")

    def test_another_agents_home_is_unreachable(self):
        self.allowed("true")
        other = self.storage / "agents/claude/home"
        other.mkdir(parents=True, exist_ok=True)
        (other / "credentials").write_text("claude token\n")
        self.refused("cat '" + str(other / "credentials") + "'")
        self.refused("echo x > '" + str(other / "planted") + "'")

    def test_host_run_files_in_the_project_are_write_protected(self):
        for target in (".srt-agent.jsonc", ".git/config", ".git/hooks/pre-commit", ".envrc", ".vscode/tasks.json"):
            with self.subTest(target=target):
                self.write_project({})  # a failed protection must not break the next launch
                self.refused("mkdir -p \"$(dirname '" + target + "')\" 2>/dev/null; echo x >> '" + target + "'")
        self.assertFalse((self.project / ".git/hooks/pre-commit").exists())
        self.assertEqual((self.project / ".git/config").read_text(), "[core]\n")

    def test_the_policy_files_are_write_protected(self):
        self.allowed("true")
        policies = self.home / ".local/state/srt-agent/policies"
        self.refused("ls '" + str(policies) + "'")

    # A project that extends a group gets exactly the group's grants.

    def test_an_extended_group_grants_its_paths_and_no_more(self):
        self.refused("cat '" + str(self.home / "shared-data/file.txt") + "'")
        self.write_project({"extends": ["data"]})
        self.allowed("cat '" + str(self.home / "shared-data/file.txt") + "'")
        self.refused("cat '" + str(self.home / "private/file.txt") + "'")
        self.refused("echo x > '" + str(self.home / "shared-data/planted") + "'")

    # Network: no direct connections; only allowed domains, through srt's proxy.

    @unittest.skipUnless(NETWORK, "SRT_AGENT_INTEGRATION_NETWORK=0")
    def test_direct_connections_are_refused(self):
        self.refused("/usr/bin/curl -sS --noproxy '*' --max-time 10 -o /dev/null https://1.1.1.1")

    @unittest.skipUnless(NETWORK, "SRT_AGENT_INTEGRATION_NETWORK=0")
    def test_an_allowed_domain_works_through_the_proxy(self):
        self.allowed("/usr/bin/curl -sS --fail --max-time 30 -o /dev/null https://example.com")

    @unittest.skipUnless(NETWORK, "SRT_AGENT_INTEGRATION_NETWORK=0")
    def test_other_domains_are_refused(self):
        self.refused("/usr/bin/curl -sS --fail --max-time 30 -o /dev/null https://example.net")

    @unittest.skipUnless(NETWORK, "SRT_AGENT_INTEGRATION_NETWORK=0")
    def test_an_extended_group_grants_its_domains(self):
        self.refused("/usr/bin/curl -sS --fail --max-time 30 -o /dev/null https://example.org")
        self.write_project({"extends": ["data"]})
        self.allowed("/usr/bin/curl -sS --fail --max-time 30 -o /dev/null https://example.org")


if __name__ == "__main__":
    unittest.main(verbosity=2)
