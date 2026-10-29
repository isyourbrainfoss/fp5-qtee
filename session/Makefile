CC ?= gcc
CFLAGS ?= -O2 -Wall

SESSION_SRC = fp5-qtee-session.c fp5_cmd.c fp5_finger.c fp5_fw.c fp5_image.c fp5_tee.c fp5_wire.c

.PHONY: all test clean install

PREFIX ?= /usr
LIBEXECDIR ?= $(PREFIX)/libexec/fp5-qtee

all: fp5-qtee-session fp5-qtee-load

fp5-qtee-load: fp5-qtee-load.c fp5_fw.c fp5_image.c fp5_tee.c fp5_fw.h fp5_image.h fp5_tee.h
	$(CC) $(CFLAGS) -o $@ fp5-qtee-load.c fp5_fw.c fp5_image.c fp5_tee.c

test_fp5_fw: test_fp5_fw.c fp5_fw.c fp5_image.c fp5_fw.h fp5_image.h
	$(CC) $(CFLAGS) -o $@ test_fp5_fw.c fp5_fw.c fp5_image.c

install: all
	install -Dm755 fp5-qtee-session $(DESTDIR)$(LIBEXECDIR)/fp5-qtee-session
	install -Dm755 fp5-qtee-load $(DESTDIR)$(LIBEXECDIR)/fp5-qtee-load

fp5-qtee-session: $(SESSION_SRC) fp5_cmd.h fp5_finger.h fp5_fw.h fp5_image.h fp5_tee.h fp5_wire.h
	$(CC) $(CFLAGS) -o $@ $(SESSION_SRC)

test_fp5_wire: test_fp5_wire.c fp5_wire.c fp5_wire.h
	$(CC) $(CFLAGS) -o $@ test_fp5_wire.c fp5_wire.c

test_fp5_image: test_fp5_image.c fp5_image.c fp5_image.h
	$(CC) $(CFLAGS) -o $@ test_fp5_image.c fp5_image.c

test_fp5_cmd: test_fp5_cmd.c fp5_cmd.c fp5_cmd.h
	$(CC) $(CFLAGS) -o $@ test_fp5_cmd.c fp5_cmd.c

test_fp5_finger: test_fp5_finger.c fp5_finger.c fp5_finger.h
	$(CC) $(CFLAGS) -o $@ test_fp5_finger.c fp5_finger.c

test: test_fp5_wire test_fp5_image test_fp5_cmd test_fp5_finger test_fp5_fw
	./test_fp5_fw
	./test_fp5_wire
	./test_fp5_image
	./test_fp5_cmd
	./test_fp5_finger

clean:
	rm -f fp5-qtee-session fp5-qtee-load test_fp5_fw test_fp5_wire test_fp5_image test_fp5_cmd test_fp5_finger
