# srt-agent-notify: show @srt_notice in the pane's top border, clear it on focus.
# Source this after any theme or plugin that sets pane-border-status or
# pane-border-format, or theirs will replace it.
set -g focus-events on
set -g pane-border-status top
# The part after the notice is tmux's default pane-border-format. If you
# already set pane-border-format, keep the notice prefix and replace the rest
# with your own format.
set -g pane-border-format '#{?@srt_notice,#[fg=black bg=yellow bold] #{@srt_notice} #[default] ,}#{?pane_active,#[reverse],}#{pane_index}#[default] "#{pane_title}"'
# On focus: clear this pane's banner. If the notifier posted a macOS
# notification for this pane (@srt_alert), remove it. run-shell uses the tmux
# server's PATH, which must contain alerter.
set-hook -g pane-focus-in {
  set-option -pu @srt_notice
  if-shell -F '#{@srt_alert}' {
    set-option -pu @srt_alert
    run-shell -b 'alerter --remove "srt-agent-#{pid}-#{pane_id}" >/dev/null 2>&1'
  }
}
