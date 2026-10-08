CC ?= gcc
CFLAGS ?= -O2
# Warnings are kept apart from CFLAGS so overriding CFLAGS keeps them.
# Build with WERROR= to turn warnings back into warnings on a newer compiler.
WARNFLAGS ?= -Wall -Wextra
WERROR ?= -Werror
# logf/logl in fp5-qtee-session.c shadow libm builtins on purpose.
ALL_CFLAGS = $(CFLAGS) $(WARNFLAGS) $(WERROR) -fno-builtin-logf -fno-builtin-logl -pthread
LDFLAGS ?=
ALL_LDFLAGS = $(LDFLAGS) -pthread
ASAN_FLAGS = -O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -fno-sanitize=alignment

SESSION_SRC = fp5-qtee-session.c fp5_image.c fp5_tee.c fp5_wire.c
LOAD_SRC = fp5-qtee-load.c fp5_image.c fp5_tee.c

.PHONY: all test test-asan clean

all: fp5-qtee-session fp5-qtee-load

fp5-qtee-session: $(SESSION_SRC) fp5_image.h fp5_tee.h fp5_wire.h
	$(CC) $(ALL_CFLAGS) -o $@ $(SESSION_SRC) $(ALL_LDFLAGS) $(LDLIBS)

fp5-qtee-load: $(LOAD_SRC) fp5_image.h fp5_tee.h
	$(CC) $(ALL_CFLAGS) -o $@ $(LOAD_SRC) $(ALL_LDFLAGS) $(LDLIBS)

test_fp5_wire: test_fp5_wire.c fp5_wire.c fp5_wire.h
	$(CC) $(ALL_CFLAGS) -o $@ test_fp5_wire.c fp5_wire.c $(ALL_LDFLAGS) $(LDLIBS)

test_fp5_image: test_fp5_image.c fp5_image.c fp5_image.h
	$(CC) $(ALL_CFLAGS) -o $@ test_fp5_image.c fp5_image.c $(ALL_LDFLAGS) $(LDLIBS)

test: test_fp5_wire test_fp5_image
	./test_fp5_wire
	./test_fp5_image

# Host-only: the same tests under AddressSanitizer and UBSan.
test-asan: test_fp5_wire.c fp5_wire.c fp5_wire.h test_fp5_image.c fp5_image.c fp5_image.h
	$(CC) $(ASAN_FLAGS) $(WARNFLAGS) -o test_fp5_wire-asan test_fp5_wire.c fp5_wire.c
	$(CC) $(ASAN_FLAGS) $(WARNFLAGS) -o test_fp5_image-asan test_fp5_image.c fp5_image.c
	./test_fp5_wire-asan
	./test_fp5_image-asan

clean:
	rm -f fp5-qtee-session fp5-qtee-load test_fp5_wire test_fp5_image \
		test_fp5_wire-asan test_fp5_image-asan
