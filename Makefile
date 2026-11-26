# Run on the host, never inside a sandbox: install uses sudo.
#   make test      run the notifier and launcher tests
#   make integration  run probes through the real srt and Seatbelt (host only)
#   make diff      show what install would change
#   make install   run test and every integration probe (filesystem, then
#                  network), then copy the programs root-owned into /usr/local
#   make install SKIP_NETWORK_CHECK=1  leave out the network probes
#   make install SKIP_INTEGRATION=1    leave out every probe (srt itself is broken)
PREFIX = /usr/local
BIN = srt-agent srt-monitor srt-agent-update-tools
LIBEXEC = srt-agent-notify

# Before installing, check that the policy the launcher about to be
# installed writes is actually enforced. The network probes run last and
# separately: they reach real sites, so when only they fail, the message
# says how to install without them.
INSTALL_CHECKS = test integration-offline integration-network
ifeq ($(SKIP_NETWORK_CHECK),1)
INSTALL_CHECKS = test integration-offline
endif
ifeq ($(SKIP_INTEGRATION),1)
INSTALL_CHECKS = test
endif

.PHONY: test integration integration-offline integration-network diff install

test:
	/usr/bin/python3 -I tests/test_srt_agent_notify.py
	/usr/bin/python3 -I tests/test_srt_agent_launcher.py

integration:
	/usr/bin/python3 -I tests/integration_srt.py

integration-offline:
	SRT_AGENT_INTEGRATION_NETWORK=0 /usr/bin/python3 -I tests/integration_srt.py

# Run after integration-offline, so a failure here is the network probes alone.
integration-network: $(filter integration-offline,$(INSTALL_CHECKS))
	@SRT_AGENT_INTEGRATION_NETWORK=only /usr/bin/python3 -I tests/integration_srt.py || { \
		echo; \
		echo "srt-agent: only the network probes failed; the filesystem probes passed."; \
		echo "If the cause is your connection or the test sites (example.com, example.org,"; \
		echo "example.net, 1.1.1.1) rather than the sandbox, install without them:"; \
		echo; \
		echo "    make install SKIP_NETWORK_CHECK=1"; \
		echo; \
		exit 1; }

diff:
	-@for f in $(BIN); do diff -u $(PREFIX)/bin/$$f bin/$$f; done
	-@for f in $(LIBEXEC); do diff -u $(PREFIX)/libexec/$$f libexec/$$f; done

install: $(INSTALL_CHECKS)
	test -d $(PREFIX)/libexec || sudo install -d -o root -g wheel -m 755 $(PREFIX)/libexec
	sudo install -o root -g wheel -m 755 $(addprefix bin/,$(BIN)) $(PREFIX)/bin/
	sudo install -o root -g wheel -m 755 $(addprefix libexec/,$(LIBEXEC)) $(PREFIX)/libexec/
	@stat -f '%Su:%Sg %Sp %N' $(PREFIX) $(PREFIX)/bin $(PREFIX)/libexec \
		$(addprefix $(PREFIX)/bin/,$(BIN)) $(addprefix $(PREFIX)/libexec/,$(LIBEXEC))
