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

/*
 * One serve-mode auth attempt. The session feeds it every command that
 * arrives while the attempt runs, every irq_count it reads, and the
 * final match verdict, and asks it what to do. Pure, so the cancel and
 * re-arm rules have host tests.
 *
 * - A new attempt starts from the irq_count read at arm time. An irq
 *   change while cancelled is ignored. A finger already down is not an
 *   irq change; the session queries once and captures only an exact
 *   down. cancel still refuses that capture. The down and cancel flags
 *   of the previous attempt are cleared.
 * - cancel or quit marks the attempt cancelled. From then on an irq
 *   change is ignored, and a match is reported as cancelled, never as
 *   a hit.
 * - auth while an attempt runs is rejected (FP5_REPLY_BUSY) rather
 *   than silently dropped; unknown lines get FP5_REPLY_UNKNOWN.
 */
struct fp5_attempt {
	unsigned irq;	/* irq_count baseline */
	int down;	/* a finger-down was taken in this attempt */
	int cancelled;	/* cancel or quit arrived */
	int quit;	/* quit (or stdin EOF) arrived */
};

enum {
	FP5_REPLY_NONE = 0,
	FP5_REPLY_BUSY,		/* print "SERVE reject auth busy" */
	FP5_REPLY_UNKNOWN,	/* print "SERVE reject unknown" */
};

/* auth_once return codes, as printed in "SERVE result <rc>". */
enum {
	FP5_ATT_HIT = 0,
	FP5_ATT_MISS = 2,
	FP5_ATT_CANCELLED = 3,
	FP5_ATT_NONE = 4,	/* armed, no finger-down in this window */
};

void fp5_att_begin(struct fp5_attempt *a, unsigned irq_now);
int fp5_att_cmd(struct fp5_attempt *a, int cmd);
/* 1 when irq_now is a new interrupt for a live attempt (moves the
 * baseline). 0 when unchanged or the attempt is cancelled. */
int fp5_att_irq(struct fp5_attempt *a, unsigned irq_now);
/* A finger-down was read. 1 if the attempt may go on to capture. */
int fp5_att_down(struct fp5_attempt *a);
/* Verdict after capture and report: HIT only for a live match. */
int fp5_att_finish(const struct fp5_attempt *a, int matched);

#endif
