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
	FP5_ATT_AGAIN = 5,	/* all-zero miss: re-arm in this process */
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

/*
 * Held miss (Android: a wrong finger held still is one miss, one buzz,
 * one strike). In touch mode the chip only raises an irq on finger-down,
 * never on lift (0 up edges in the Oct 26 logs), but a fresh AUTH_ARM +
 * WMODE_TOUCH + QEV reports a finger that is still there as down (rc 5,
 * itype 0x2). So after a press that ended without a hit:
 * - the down found by the arm query (or a panel/resume query) is that
 *   same finger: wait, do not capture;
 * - re-arm and query every fp5_lift_check_ms() (200 ms, later 0.5/2 s) until the query says no
 *   finger: that is the lift. Only then a new down is a new press;
 * - a down irq edge at least FP5_LIFT_EDGE_MS after the last re-arm is a
 *   new touch (the chip saw a lift we could not), so it is a press.
 * A hit or a cancel does not ask for a lift.
 */
#define FP5_LIFT_EDGE_MS 300
enum {
	FP5_GATE_NONE = 0,	/* no finger-down in this sample */
	FP5_GATE_PRESS,		/* new press: capture */
	FP5_GATE_HELD,		/* the finger from the last miss: wait */
};
/* 1 when the next attempt must see the finger lifted first. */
int fp5_lift_after(int result);
int fp5_lift_gate(int need_lift, int from_query, int real_down,
		  long ms_since_arm);
/* Re-arm check interval while a missed finger stays down for held_ms. */
int fp5_lift_check_ms(long held_ms);

/*
 * Shared attempts (fp5-fprintd verifying through the watcher's kept serve
 * session, see docs/fprintd-share.md). A root client on the share socket
 * sends "auth"; the session runs one attempt for it with its output going
 * to that client only. The watcher (stdin) always wins:
 * - watcher auth during a shared attempt: cancel the shared attempt and
 *   run the watcher's auth right after it (pending_auth);
 * - watcher quit or EOF: cancel and leave serve mode;
 * - watcher cancel: nothing to cancel, unless its auth is pending; then
 *   that auth runs with the cancel applied at once (pending_auth 2), as
 *   if "auth\ncancel\n" had come in one write;
 * - client cancel: cancel; client quit or EOF: cancel and drop the client;
 * - client auth while its attempt runs: busy.
 * Return value as fp5_att_cmd.
 */
int fp5_share_from_watcher(struct fp5_attempt *a, int cmd, int *pending_auth);
int fp5_share_from_client(struct fp5_attempt *a, int cmd, int *drop_client);
/* Put one line back in front of what is queued (the pending cancel).
 * 0 if it does not fit; then nothing changes. */
int fp5_cmd_unshift(struct fp5_cmdbuf *b, const char *line);
/* Who goes next when the session is idle. 1: the watcher's line,
 * 2: the client's line, 0: nothing. The watcher always comes first. */
int fp5_share_pick(int watcher_has_line, int client_has_line);
/* Attempt numbers for shared attempts start here, so they never look
 * like one of the watcher's numbered auths. */
#define FP5_SHARE_FIRST 100000

#endif
