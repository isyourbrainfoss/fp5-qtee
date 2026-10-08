CC ?= gcc
CFLAGS ?= -O2 -Wall

SESSION_SRC = fp5-qtee-session.c fp5_image.c fp5_tee.c fp5_wire.c

.PHONY: all test clean

all: fp5-qtee-session

fp5-qtee-session: $(SESSION_SRC) fp5_image.h fp5_tee.h fp5_wire.h
	$(CC) $(CFLAGS) -o $@ $(SESSION_SRC)

test_fp5_wire: test_fp5_wire.c fp5_wire.c fp5_wire.h
	$(CC) $(CFLAGS) -o $@ test_fp5_wire.c fp5_wire.c

test_fp5_image: test_fp5_image.c fp5_image.c fp5_image.h
	$(CC) $(CFLAGS) -o $@ test_fp5_image.c fp5_image.c

test: test_fp5_wire test_fp5_image
	./test_fp5_wire
	./test_fp5_image

clean:
	rm -f fp5-qtee-session fp5-qtee-load test_fp5_wire test_fp5_image
