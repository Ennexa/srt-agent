# Run on the host, never inside a sandbox: install uses sudo.
#   make test      run the notifier and launcher tests
#   make integration  run probes through the real srt and Seatbelt (host only)
#   make diff      show what install would change
#   make install   run test and the offline integration probes, then copy
#                  the programs root-owned into /usr/local
#   make install SKIP_INTEGRATION=1   skip the probes (e.g. srt itself is broken)
PREFIX = /usr/local
BIN = srt-agent srt-monitor srt-agent-update-tools
LIBEXEC = srt-agent-notify

# Before installing, check that the policy the launcher about to be
# installed writes is actually enforced. Network probes are left out: they
# depend on outside sites, and launcher changes mostly touch the filesystem.
INSTALL_CHECKS = test integration-offline
ifeq ($(SKIP_INTEGRATION),1)
INSTALL_CHECKS = test
endif

.PHONY: test integration integration-offline diff install

test:
	/usr/bin/python3 -I tests/test_srt_agent_notify.py
	/usr/bin/python3 -I tests/test_srt_agent_launcher.py

integration:
	/usr/bin/python3 -I tests/integration_srt.py

integration-offline:
	SRT_AGENT_INTEGRATION_NETWORK=0 /usr/bin/python3 -I tests/integration_srt.py

diff:
	-@for f in $(BIN); do diff -u $(PREFIX)/bin/$$f bin/$$f; done
	-@for f in $(LIBEXEC); do diff -u $(PREFIX)/libexec/$$f libexec/$$f; done

install: $(INSTALL_CHECKS)
	test -d $(PREFIX)/libexec || sudo install -d -o root -g wheel -m 755 $(PREFIX)/libexec
	sudo install -o root -g wheel -m 755 $(addprefix bin/,$(BIN)) $(PREFIX)/bin/
	sudo install -o root -g wheel -m 755 $(addprefix libexec/,$(LIBEXEC)) $(PREFIX)/libexec/
	@stat -f '%Su:%Sg %Sp %N' $(PREFIX) $(PREFIX)/bin $(PREFIX)/libexec \
		$(addprefix $(PREFIX)/bin/,$(BIN)) $(addprefix $(PREFIX)/libexec/,$(LIBEXEC))
