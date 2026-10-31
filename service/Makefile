# Install the system-service layout (BengalOS: no sudo, read-only /usr).
# make -C session && make -C service install DESTDIR=...
PREFIX ?= /usr
LIBEXECDIR ?= $(PREFIX)/libexec/fp5-qtee
DATADIR ?= $(PREFIX)/share/fp5-qtee
UNITDIR ?= $(PREFIX)/lib/systemd/system
USERUNITDIR ?= $(PREFIX)/lib/systemd/user
POLKITDIR ?= $(PREFIX)/share/polkit-1/actions
DOCDIR ?= $(PREFIX)/share/doc/fp5-qtee
APP = ../app
PY = fp5_qtee_unlock.py fp5_qtee_transport.py fp5_qtee_haptic.py fp5_qtee_coach.py fp5-qtee-app.py

.PHONY: install
install:
	$(MAKE) -C ../session install DESTDIR=$(DESTDIR) PREFIX=$(PREFIX) LIBEXECDIR=$(LIBEXECDIR)
	install -Dm755 fp5-qtee-broker $(DESTDIR)$(LIBEXECDIR)/fp5-qtee-broker
	install -Dm644 fp5-qtee.socket $(DESTDIR)$(UNITDIR)/fp5-qtee.socket
	install -Dm644 fp5-qtee@.service $(DESTDIR)$(UNITDIR)/fp5-qtee@.service
	install -Dm644 fp5-fprintd.service $(DESTDIR)$(UNITDIR)/fp5-fprintd.service
	install -Dm755 ../fprintd/fp5_fprintd.py $(DESTDIR)$(LIBEXECDIR)/fp5-fprintd
	install -Dm644 net.reactivated.Fprint.conf $(DESTDIR)$(PREFIX)/share/dbus-1/system.d/net.reactivated.Fprint.conf
	install -Dm644 net.reactivated.Fprint.service $(DESTDIR)$(PREFIX)/share/dbus-1/system-services/net.reactivated.Fprint.service
	install -Dm644 ../fprintd/net.reactivated.fprint.device.policy $(DESTDIR)$(POLKITDIR)/net.reactivated.fprint.device.policy
	install -Dm644 ../docs/fprintd.md $(DESTDIR)$(DOCDIR)/fprintd.md
	install -Dm644 fp5-qtee-unlock.service $(DESTDIR)$(USERUNITDIR)/fp5-qtee-unlock.service
	install -Dm644 org.fp5.qtee.policy $(DESTDIR)$(POLKITDIR)/org.fp5.qtee.policy
	install -Dm644 org.fp5.qtee.desktop $(DESTDIR)$(PREFIX)/share/applications/org.fp5.qtee.desktop
	install -Dm644 $(APP)/fp5-qtee-feedback.json $(DESTDIR)$(DATADIR)/fp5-qtee-feedback.json
	for f in $(PY); do install -Dm644 $(APP)/$$f $(DESTDIR)$(DATADIR)/$$f; done
	install -Dm644 ../docs/bengalos-mbn.md $(DESTDIR)$(DOCDIR)/bengalos-mbn.md
	install -Dm644 99-fp5-qtee.preset $(DESTDIR)$(PREFIX)/lib/systemd/system-preset/99-fp5-qtee.preset
	install -Dm644 99-fp5-qtee-user.preset $(DESTDIR)$(PREFIX)/lib/systemd/user-preset/99-fp5-qtee.preset
