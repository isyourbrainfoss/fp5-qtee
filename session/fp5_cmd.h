#ifndef FP5_CMD_H
#define FP5_CMD_H

#include <stddef.h>

/*
 * Line commands for "serve" mode, read from stdin. Pure, so the host
 * tests can drive it without a phone.
 */
enum {
	FP5_CMD_NONE = 0,	/* no full line yet */
	FP5_CMD_AUTH,		/* arm one match */
	FP5_CMD_CANCEL,		/* stop waiting for a finger */
	FP5_CMD_QUIT,		/* leave serve mode (also stdin EOF) */
	FP5_CMD_UNKNOWN,
};

#define FP5_CMD_LINE 64

struct fp5_cmdbuf {
	char buf[FP5_CMD_LINE];
	size_t n;
	int skipping;	/* inside a line that did not fit; drop to newline */
};

void fp5_cmd_push(struct fp5_cmdbuf *b, const char *data, size_t len);
/* Copy the next whole line (without newline) to out. 1 if there was one. */
int fp5_cmd_next(struct fp5_cmdbuf *b, char *out, size_t cap);
int fp5_cmd_parse(const char *line);

#endif
