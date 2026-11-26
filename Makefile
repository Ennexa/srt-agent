# Run on the host, never inside a sandbox: install uses sudo.
#   make test      run the notifier and launcher tests
#   make integration  run probes through the real srt and Seatbelt (host only)
#   make diff      show what install would change
#   make install   copy the programs root-owned into /usr/local
PREFIX = /usr/local
BIN = srt-agent srt-monitor srt-agent-update-tools
LIBEXEC = srt-agent-notify

.PHONY: test integration diff install

test:
	/usr/bin/python3 -I tests/test_srt_agent_notify.py
	/usr/bin/python3 -I tests/test_srt_agent_launcher.py

integration:
	/usr/bin/python3 -I tests/integration_srt.py

diff:
	-@for f in $(BIN); do diff -u $(PREFIX)/bin/$$f bin/$$f; done
	-@for f in $(LIBEXEC); do diff -u $(PREFIX)/libexec/$$f libexec/$$f; done

install: test
	test -d $(PREFIX)/libexec || sudo install -d -o root -g wheel -m 755 $(PREFIX)/libexec
	sudo install -o root -g wheel -m 755 $(addprefix bin/,$(BIN)) $(PREFIX)/bin/
	sudo install -o root -g wheel -m 755 $(addprefix libexec/,$(LIBEXEC)) $(PREFIX)/libexec/
	@stat -f '%Su:%Sg %Sp %N' $(PREFIX) $(PREFIX)/bin $(PREFIX)/libexec \
		$(addprefix $(PREFIX)/bin/,$(BIN)) $(addprefix $(PREFIX)/libexec/,$(LIBEXEC))
