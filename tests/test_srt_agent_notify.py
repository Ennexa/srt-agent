"""Tests for srt-agent-notify, with fake tmux, lsappinfo, wezterm and alerter on PATH.

Run: make test   (or: /usr/bin/python3 -I tests/test_srt_agent_notify.py)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "libexec", "srt-agent-notify")
WEZTERM = "com.github.wez.wezterm"
SOCK = ["-S", "/fake/tmux.sock"]

LOG = """#!/bin/sh
{ printf '%s\\n' "$@"; echo ---; } >> "$FAKE_LOG/NAME"
"""
STUBS = {
    "tmux": LOG + """for last; do :; done
case " $* " in
  *" display-message "*) [ -n "$FAKE_TMUX_STATE" ] || exit 1
    if [ "$last" = '#{session_id}' ]; then echo '$1'; else printf '%s\\n' "$FAKE_TMUX_STATE"; fi;;
  *" list-clients "*) if [ "$last" = '#{client_flags}' ]; then out=$FAKE_TMUX_CLIENTS; else out=$FAKE_TMUX_TTYS; fi
    [ -n "$out" ] && printf '%s\\n' "$out"; exit 0;;
  *" set-option "*) [ -n "$FAKE_TMUX_STATE" ] || exit 1; exit "${FAKE_TMUX_SET_RC:-0}";;
esac
""",
    "lsappinfo": LOG + """case "$1" in
  front) echo "ASN:0x0-0x1a2b:";;
  info) echo "\\"CFBundleIdentifier\\"=\\"$FAKE_FRONT\\"";;
esac
""",
    "wezterm": LOG + """case " $* " in
  *" list "*) printf '%s\\n' "$FAKE_WEZTERM_LIST";;
  *" list-clients "*) printf '%s\\n' "$FAKE_WEZTERM_JSON";;
esac
""",
    "alerter": LOG + """printf '%s' "$FAKE_ALERTER_OUT"
""",
    "open": LOG,
}
CLICK = '{"activationType": "contentsClicked"}'


def payload(event="stop", hook="Stop", pane="%12", **over):
    d = {"hook_event_name": hook, "session_id": "ab12cd", "cwd": "/p/proj",
         "message": "claude finished in proj",
         "srt_agent": {"event": event, "agent": "claude", "project": "/p/proj",
                       "tmux_pane": pane}}
    d.update(over)
    return json.dumps(d)


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        for name, body in STUBS.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body.replace("NAME", name))
            os.chmod(path, 0o755)
        # A copy of the script whose absolute wezterm path points at the fake.
        with open(SCRIPT) as f:
            source = f.read()
        real = '"/Applications/WezTerm.app/Contents/MacOS/wezterm"'
        self.assertIn(real, source)
        self.script = os.path.join(self.dir, "srt-agent-notify")
        with open(self.script, "w") as f:
            f.write(source.replace(real, repr(os.path.join(self.bin, "wezterm"))))
        # Default: tmux pane %12, focused, in a focused client.
        self.env = {"PATH": self.bin, "FAKE_LOG": self.dir, "TMUX": "/fake/tmux.sock,123,0",
                    "FAKE_TMUX_STATE": "%12 1 1 $1", "FAKE_TMUX_CLIENTS": "attached,focused,UTF-8",
                    "FAKE_FRONT": WEZTERM, "FAKE_WEZTERM_JSON": '[{"focused_pane_id": 3}]'}

    def tearDown(self):
        self.tmp.cleanup()

    def run_notify(self, stdin, **env):
        e = dict(self.env, **env)
        for k in [k for k, v in e.items() if v is None]:
            del e[k]
        p = subprocess.run([sys.executable, "-I", self.script], input=stdin, env=e,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertNotIn("Traceback", p.stderr)
        self.stderr = p.stderr

    def run_wezterm(self, **env):
        """Run outside tmux, in WezTerm pane 3."""
        self.run_notify(payload(pane=""), **dict({"TMUX": None, "WEZTERM_PANE": "3"}, **env))

    def calls(self, name, wait=False):
        path = os.path.join(self.dir, name)
        deadline = time.time() + (5 if wait else 0)
        while wait and not os.path.exists(path) and time.time() < deadline:
            time.sleep(0.05)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            text = f.read()
        return [c.strip("\n").split("\n") for c in text.split("---\n") if c.strip("\n")]

    def assert_alerter(self, group, persistent=True):
        """One alerter call. Persistent means @srt_alert was set and there's no timeout."""
        calls = self.calls("alerter", wait=True)
        self.assertEqual(len(calls), 1)
        args = calls[0]
        i = args.index("--title")
        self.assertEqual(args[i:], ["--title", "claude", "--message", "claude finished in proj",
                                    "--group", group, "--json"]
                         + ([] if persistent else ["--timeout", "30"]))
        self.assertNotIn("--sender", args)
        if persistent:
            self.assertEqual([c for c in self.calls("tmux") if "set-option" in c],
                             [SOCK + ["set-option", "-p", "-t", "%12", "@srt_alert", "1"]])

    def assert_silent(self):
        self.assertEqual(self.calls("alerter"), [])
        self.assertFalse([c for c in self.calls("tmux") if "set-option" in c])

    # In tmux.
    def test_focused_is_silent(self):
        self.run_notify(payload())
        self.assert_silent()
        self.assertEqual(self.stderr, "tmux pane %12 focused: silent\n")
        self.assertEqual(self.calls("tmux"), [
            SOCK + ["display-message", "-p", "-t", "%12",
                    "#{pane_id} #{pane_active} #{window_active} #{session_id}"],
            SOCK + ["list-clients", "-t", "$1", "-F", "#{client_flags}"]])
        self.assertEqual(self.calls("lsappinfo") + self.calls("wezterm"), [])

    def test_one_of_two_clients_focused_is_silent(self):
        self.run_notify(payload(), FAKE_TMUX_CLIENTS="attached,UTF-8\nattached,focused")
        self.assert_silent()

    def test_visible_sets_banner(self):
        self.run_notify(payload("failure", "StopFailure"), FAKE_TMUX_STATE="%12 0 1 $1")
        self.assertEqual(self.calls("tmux")[2],
                         SOCK + ["set-option", "-p", "-t", "%12", "@srt_notice", "Failed"])
        self.assertEqual(self.calls("alerter"), [])

    def test_other_window_notifies(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="%12 1 0 $1")
        self.assert_alerter("srt-agent-123-%12")

    def test_unfocused_client_notifies(self):
        # Other terminal tab, or the terminal isn't frontmost.
        self.run_notify(payload(), FAKE_TMUX_CLIENTS="attached,UTF-8")
        self.assert_alerter("srt-agent-123-%12")

    def test_detached_session_notifies(self):
        self.run_notify(payload(), FAKE_TMUX_CLIENTS="")
        self.assert_alerter("srt-agent-123-%12")

    def test_tmux_in_wezterm_ignores_wezterm(self):
        self.run_notify(payload(), WEZTERM_PANE="3", FAKE_TMUX_CLIENTS="attached")
        self.assert_alerter("srt-agent-123-%12")
        self.assertEqual(self.calls("lsappinfo") + self.calls("wezterm"), [])

    def test_tmux_in_wezterm_focused_is_silent(self):
        self.run_notify(payload(), WEZTERM_PANE="7")
        self.assert_silent()
        self.assertEqual(self.calls("wezterm"), [])

    # WezTerm outside tmux.
    def test_wezterm_focused_pane_is_silent(self):
        self.run_wezterm()
        self.assert_silent()
        self.assertEqual(self.calls("wezterm"),
                         [["cli", "--no-auto-start", "list-clients", "--format", "json"]])
        self.assertEqual(self.calls("tmux"), [])

    def test_wezterm_other_pane_notifies(self):
        self.run_wezterm(FAKE_WEZTERM_JSON='[{"focused_pane_id": 4}, {"focused_pane_id": null}]')
        self.assert_alerter("srt-agent-ab12cd", persistent=False)

    def test_wezterm_not_frontmost_notifies(self):
        self.run_wezterm(FAKE_FRONT="com.apple.Safari")
        self.assert_alerter("srt-agent-ab12cd", persistent=False)
        self.assertEqual(self.calls("wezterm"), [])

    def test_wezterm_bad_output_notifies(self):
        for out in ["", "not json", "{}", '[{"focused_pane_id": "3"}]', "[3]"]:
            with self.subTest(out=out):
                self.run_wezterm(FAKE_WEZTERM_JSON=out)
                self.assertEqual(len(self.calls("alerter", wait=True)), 1)
                os.remove(os.path.join(self.dir, "alerter"))

    def test_wezterm_true_is_not_pane_1(self):
        self.run_wezterm(WEZTERM_PANE="1", FAKE_WEZTERM_JSON='[{"focused_pane_id": true}]')
        self.assert_alerter("srt-agent-ab12cd", persistent=False)

    def test_bad_wezterm_pane_notifies_without_wezterm(self):
        self.run_wezterm(WEZTERM_PANE="3;x")
        self.assert_alerter("srt-agent-ab12cd", persistent=False)
        self.assertEqual(self.calls("lsappinfo") + self.calls("wezterm"), [])

    # Other fallbacks.
    def test_no_tmux_no_wezterm_notifies(self):
        self.run_notify(payload(pane=""), TMUX=None)
        self.assert_alerter("srt-agent-ab12cd", persistent=False)
        self.assertEqual(self.calls("tmux") + self.calls("lsappinfo") + self.calls("wezterm"), [])

    def test_pane_without_tmux_env_notifies(self):
        self.run_notify(payload(), TMUX=None, WEZTERM_PANE="3")
        self.assert_alerter("srt-agent-ab12cd", persistent=False)
        self.assertEqual(self.calls("tmux") + self.calls("wezterm"), [])

    def test_no_pane_in_tmux_uses_session_group(self):
        self.run_notify(payload(pane=""))
        self.assert_alerter("srt-agent-ab12cd", persistent=False)
        self.assertEqual(self.calls("tmux"), [])

    def test_null_pane_is_no_pane(self):
        self.run_notify(payload(pane=None))
        self.assert_alerter("srt-agent-ab12cd", persistent=False)

    def test_bad_server_pid_uses_session_group(self):
        self.run_notify(payload(), TMUX="/fake/tmux.sock,1;x,0", FAKE_TMUX_STATE="%12 1 0 $1")
        self.assert_alerter("srt-agent-ab12cd")

    def test_missing_pane_notifies(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="")
        self.assert_alerter("srt-agent-123-%12", persistent=False)

    def test_other_pane_reported_notifies(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="%13 1 1 $1")
        self.assert_alerter("srt-agent-123-%12")

    def test_bad_session_id_from_tmux_notifies(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="%12 1 1 x")
        self.assert_alerter("srt-agent-123-%12")
        self.assertFalse([c for c in self.calls("tmux") if "list-clients" in c])

    def test_banner_failure_notifies(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="%12 0 1 $1", FAKE_TMUX_SET_RC="1")
        self.assert_alerter("srt-agent-123-%12", persistent=False)

    # New event words.
    def test_permission_banner(self):
        self.run_notify(payload("permission", "Notification"), FAKE_TMUX_STATE="%12 0 1 $1")
        self.assertEqual(self.calls("tmux")[2],
                         SOCK + ["set-option", "-p", "-t", "%12", "@srt_notice", "Needs permission"])

    def test_idle_notifies(self):
        self.run_notify(payload("idle", "Notification"), FAKE_TMUX_STATE="%12 1 0 $1")
        self.assert_alerter("srt-agent-123-%12")

    # Click to jump.
    def wait_for(self, name, test):
        deadline = time.time() + 5
        while time.time() < deadline:
            calls = self.calls(name)
            if test(calls):
                return calls
            time.sleep(0.05)
        return self.calls(name)

    WEZ_LIST = ('[{"pane_id": 5, "tab_id": 2, "tty_name": "/dev/ttys002"},'
                ' {"pane_id": 3, "tab_id": 7, "tty_name": "/dev/ttys001"},'
                ' {"pane_id": "9", "tab_id": 1, "tty_name": "/dev/ttys009"}]')
    WEZ = ["cli", "--no-auto-start"]

    def jump_calls(self):
        return [c for c in self.calls("tmux") if c[2] in ("switch-client", "select-window", "select-pane")]

    def test_click_jumps_to_tmux_pane_and_wezterm_tab(self):
        # The most recently active client (ttys002) is the one shown, in its WezTerm tab.
        self.run_notify(payload(), FAKE_TMUX_STATE="%12 1 0 $1", FAKE_ALERTER_OUT=CLICK,
                        FAKE_TMUX_TTYS="100 /dev/ttys001\n200 /dev/ttys002\nbad line",
                        FAKE_WEZTERM_LIST=self.WEZ_LIST, __CFBundleIdentifier=WEZTERM)
        self.assertEqual(self.wait_for("open", bool), [["-b", WEZTERM]])
        self.assertEqual(self.jump_calls(), [
            SOCK + ["switch-client", "-c", "/dev/ttys002", "-t", "%12"],
            SOCK + ["select-window", "-t", "%12"], SOCK + ["select-pane", "-t", "%12"]])
        self.assertEqual(self.calls("wezterm"), [
            self.WEZ + ["list", "--format", "json"],
            self.WEZ + ["activate-tab", "--tab-id", "2"],
            self.WEZ + ["activate-pane", "--pane-id", "5"]])

    def test_click_without_client_tty_skips_wezterm(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="%12 1 0 $1", FAKE_ALERTER_OUT=CLICK,
                        FAKE_TMUX_TTYS="", __CFBundleIdentifier=WEZTERM)
        self.assertEqual(self.wait_for("open", bool), [["-b", WEZTERM]])
        self.assertEqual(self.jump_calls()[0], SOCK + ["switch-client", "-t", "%12"])
        self.assertEqual(self.calls("wezterm"), [])

    def test_click_with_unknown_tty_activates_nothing(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="%12 1 0 $1", FAKE_ALERTER_OUT=CLICK,
                        FAKE_TMUX_TTYS="100 /dev/ttys044", FAKE_WEZTERM_LIST=self.WEZ_LIST,
                        __CFBundleIdentifier=WEZTERM)
        self.assertEqual(self.wait_for("open", bool), [["-b", WEZTERM]])
        self.assertEqual(self.calls("wezterm"), [self.WEZ + ["list", "--format", "json"]])

    def test_click_jumps_to_wezterm_pane(self):
        self.run_wezterm(FAKE_FRONT="com.apple.Safari", FAKE_ALERTER_OUT=CLICK,
                         FAKE_WEZTERM_LIST=self.WEZ_LIST, __CFBundleIdentifier=WEZTERM)
        self.assertEqual(self.wait_for("open", bool), [["-b", WEZTERM]])
        self.assertEqual(self.calls("wezterm"), [
            self.WEZ + ["list", "--format", "json"],
            self.WEZ + ["activate-tab", "--tab-id", "7"],
            self.WEZ + ["activate-pane", "--pane-id", "3"]])

    def test_click_with_bad_wezterm_list_activates_nothing(self):
        for out in ["", "not json", "{}", '[{"pane_id": 3, "tab_id": "7"}]', '[{"pane_id": true, "tab_id": 1}]']:
            with self.subTest(out=out):
                self.run_wezterm(WEZTERM_PANE="1" if "true" in out else "3", FAKE_FRONT="x",
                                 FAKE_ALERTER_OUT=CLICK, FAKE_WEZTERM_LIST=out,
                                 __CFBundleIdentifier=WEZTERM)
                self.wait_for("open", bool)
                self.assertEqual([c for c in self.calls("wezterm") if "list" not in c], [])
                for name in ("open", "wezterm", "alerter"):
                    os.remove(os.path.join(self.dir, name))

    def test_show_button_and_old_alerter_output_jump(self):
        for out in ['{"activationType": "actionClicked", "activationValue": "Show"}',
                    "@CONTENTCLICKED", "@ACTIONCLICKED"]:
            with self.subTest(out=out):
                self.run_notify(payload(pane=""), TMUX=None, FAKE_ALERTER_OUT=out,
                                __CFBundleIdentifier=WEZTERM)
                self.assertEqual(self.wait_for("open", bool), [["-b", WEZTERM]])
                for name in ("open", "alerter"):
                    os.remove(os.path.join(self.dir, name))

    def test_no_click_no_jump(self):
        for out in ['{"activationType": "timeout"}', '{"activationType": "closed"}',
                    '{"activationType": "replied"}', "@TIMEOUT", "@CLOSED", "", "[1]"]:
            with self.subTest(out=out):
                self.run_notify(payload(), FAKE_TMUX_STATE="%12 1 0 $1", FAKE_ALERTER_OUT=out,
                                __CFBundleIdentifier=WEZTERM)
                self.calls("alerter", wait=True)
                time.sleep(0.3)
                self.assertEqual(self.calls("open"), [])
                self.assertFalse([c for c in self.calls("tmux") if "switch-client" in c])
                os.remove(os.path.join(self.dir, "alerter"))

    def test_click_with_bad_bundle_id_skips_open(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="%12 1 0 $1", FAKE_ALERTER_OUT=CLICK,
                        __CFBundleIdentifier="-a Evil")
        calls = self.wait_for("tmux", lambda c: any("select-pane" in x for x in c))
        self.assertTrue(any("select-pane" in c for c in calls))
        time.sleep(0.3)
        self.assertEqual(self.calls("open"), [])

    def test_click_on_unreachable_pane_only_raises_terminal(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="", FAKE_ALERTER_OUT=CLICK,
                        __CFBundleIdentifier=WEZTERM)
        self.assertEqual(self.wait_for("open", bool), [["-b", WEZTERM]])
        self.assertFalse([c for c in self.calls("tmux") if "switch-client" in c])

    def test_no_alerter_exits_quietly(self):
        os.remove(os.path.join(self.bin, "alerter"))
        self.run_notify(payload(), TMUX=None)
        self.assertEqual(self.stderr.splitlines()[-1], "notify: alerter not found on PATH")

    def test_decision_is_logged(self):
        self.run_notify(payload(), FAKE_TMUX_STATE="%12 1 0 $1")
        lines = self.stderr.splitlines()
        self.assertEqual(lines[0], "tmux pane %12: not on screen")
        self.assertTrue(lines[1].startswith("notify: started ") and
                        lines[1].endswith("/alerter, group srt-agent-123-%12"), lines[1])
        self.run_wezterm(FAKE_FRONT="com.apple.Safari")
        self.assertEqual(self.stderr.splitlines()[0],
                         "WezTerm pane 3 not focused or WezTerm not frontmost")

    # Bad input: nothing runs at all.
    def test_bad_input(self):
        bad = ["", "not json", "[]", '"stop"', json.dumps({"hook_event_name": "Stop"}),
               payload("bogus", "Stop"), payload("stop", "Notification"),
               payload(pane="%12; rm -rf ~"), payload(pane="12"), payload(pane=12),
               payload(session_id="../x"), payload(session_id=1),
               payload("permission", "Stop"), payload("idle", "StopFailure"),
               payload(message="--sender x"), payload(message=""), payload(message="a\nb"),
               payload(message=["x"]), payload(message="x" * 501), payload(cwd=None)]
        bad.append(payload().replace('"agent": "claude"', '"agent": "-x"'))
        bad.append(payload().replace('"project": "/p/proj"', '"project": 1'))
        for stdin in bad:
            with self.subTest(stdin=stdin):
                self.run_notify(stdin, WEZTERM_PANE="3")
                self.assertEqual(self.stderr, "")
                self.assertEqual([self.calls(n) for n in ("tmux", "lsappinfo", "wezterm", "alerter")],
                                 [[], [], [], []])


if __name__ == "__main__":
    unittest.main()
