#include "fp5_cmd.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails;

#define CHECK(c, msg) do { if (!(c)) { printf("FAIL %s\n", msg); fails++; } } while (0)

static void test_lines(void)
{
	struct fp5_cmdbuf b;
	char out[FP5_CMD_LINE];

	memset(&b, 0, sizeof(b));
	CHECK(!fp5_cmd_next(&b, out, sizeof(out)), "empty");
	fp5_cmd_push(&b, "au", 2);
	CHECK(!fp5_cmd_next(&b, out, sizeof(out)), "partial line");
	fp5_cmd_push(&b, "th\ncancel\r\nquit\n", 16);
	CHECK(fp5_cmd_next(&b, out, sizeof(out)) && fp5_cmd_parse(out) == FP5_CMD_AUTH,
	      "auth across two pushes");
	CHECK(fp5_cmd_next(&b, out, sizeof(out)) && fp5_cmd_parse(out) == FP5_CMD_CANCEL,
	      "cancel with CR");
	CHECK(fp5_cmd_next(&b, out, sizeof(out)) && fp5_cmd_parse(out) == FP5_CMD_QUIT,
	      "quit");
	CHECK(!fp5_cmd_next(&b, out, sizeof(out)) && b.n == 0, "drained");
}

static void test_parse(void)
{
	CHECK(fp5_cmd_parse("") == FP5_CMD_NONE, "blank");
	CHECK(fp5_cmd_parse("auth ") == FP5_CMD_AUTH, "trailing space");
	CHECK(fp5_cmd_parse("authx") == FP5_CMD_UNKNOWN, "authx");
	CHECK(fp5_cmd_parse("enroll") == FP5_CMD_UNKNOWN, "enroll is not a serve command");
}

static void test_overlong(void)
{
	struct fp5_cmdbuf b;
	char out[FP5_CMD_LINE];
	char big[200];

	memset(&b, 0, sizeof(b));
	memset(big, 'x', sizeof(big));
	fp5_cmd_push(&b, big, sizeof(big));
	CHECK(b.n <= sizeof(b.buf), "stays in buffer");
	fp5_cmd_push(&b, "yy\nauth\n", 8);
	CHECK(fp5_cmd_next(&b, out, sizeof(out)) && fp5_cmd_parse(out) == FP5_CMD_AUTH,
	      "over-long line dropped, next line kept");
	CHECK(!fp5_cmd_next(&b, out, sizeof(out)), "nothing else");
}

static void test_small_out(void)
{
	struct fp5_cmdbuf b;
	char out[3];

	memset(&b, 0, sizeof(b));
	fp5_cmd_push(&b, "cancel\nquit\n", 12);
	CHECK(fp5_cmd_next(&b, out, sizeof(out)) && !strcmp(out, "ca"), "truncated copy");
	CHECK(fp5_cmd_next(&b, out, sizeof(out)) && !strcmp(out, "qu"), "next line intact");
}

/* Review: cancel after a finger is down must never report a hit. */
static void test_cancel_after_down_never_hits(void)
{
	struct fp5_attempt a;

	fp5_att_begin(&a, 10);
	CHECK(fp5_att_irq(&a, 11), "fresh irq is a down candidate");
	CHECK(fp5_att_down(&a), "down taken");
	CHECK(fp5_att_cmd(&a, FP5_CMD_CANCEL) == FP5_REPLY_NONE, "cancel accepted");
	CHECK(fp5_att_finish(&a, 1) == FP5_ATT_CANCELLED, "match after cancel is not a hit");
	CHECK(fp5_att_finish(&a, 0) == FP5_ATT_MISS, "non-match after cancel is still a strike");

	/* Cancel between the QEV and the capture. */
	fp5_att_begin(&a, 10);
	CHECK(fp5_att_irq(&a, 11), "irq");
	fp5_att_cmd(&a, FP5_CMD_CANCEL);
	CHECK(!fp5_att_down(&a), "no capture once cancelled");
	CHECK(fp5_att_finish(&a, 1) != FP5_ATT_HIT, "and never a hit");

	/* quit (stdin EOF) cancels too. */
	fp5_att_begin(&a, 10);
	fp5_att_down(&a);
	fp5_att_cmd(&a, FP5_CMD_QUIT);
	CHECK(a.quit && fp5_att_finish(&a, 1) == FP5_ATT_CANCELLED, "quit cancels");

	/* Live match is a hit. */
	fp5_att_begin(&a, 10);
	CHECK(fp5_att_irq(&a, 11) && fp5_att_down(&a), "live down");
	CHECK(fp5_att_finish(&a, 1) == FP5_ATT_HIT, "live match hits");
}

/* Review: an irq change that is already there must not beat a cancel. */
static void test_cancel_beats_pending_irq(void)
{
	struct fp5_attempt a;

	fp5_att_begin(&a, 3);
	fp5_att_cmd(&a, FP5_CMD_CANCEL);
	CHECK(!fp5_att_irq(&a, 4), "irq ignored after cancel");
	CHECK(a.irq == 3, "baseline not moved by an ignored irq");
}

/* Review: a touch while cancelled / panel off must not carry into the
 * next auth. The new attempt's baseline is the irq_count at re-arm. */
static void test_touch_between_attempts_does_not_carry(void)
{
	struct fp5_attempt a;

	fp5_att_begin(&a, 3);
	fp5_att_cmd(&a, FP5_CMD_CANCEL);
	CHECK(!fp5_att_irq(&a, 5), "touch while cancelled ignored");
	/* Panel off: touches move irq_count to 7. Wake: re-arm. */
	fp5_att_begin(&a, 7);
	CHECK(!a.cancelled && !a.down, "old cancel and down cleared");
	CHECK(!fp5_att_irq(&a, 7), "old touches are in the baseline");
	CHECK(fp5_att_irq(&a, 8), "a new touch counts");
	CHECK(!fp5_att_irq(&a, 8), "and only once");
}

/* Review: an auth arriving during a wait was parsed and dropped. */
static void test_commands_during_wait(void)
{
	struct fp5_attempt a;

	fp5_att_begin(&a, 0);
	CHECK(fp5_att_cmd(&a, FP5_CMD_AUTH) == FP5_REPLY_BUSY, "auth while busy rejected");
	CHECK(!a.cancelled, "and does not cancel");
	CHECK(fp5_att_cmd(&a, FP5_CMD_UNKNOWN) == FP5_REPLY_UNKNOWN, "unknown rejected");
	CHECK(fp5_att_cmd(&a, FP5_CMD_NONE) == FP5_REPLY_NONE, "blank ignored");
}

/* "auth\ncancel\n" in one write: the cancel is applied to that auth. */
static void test_cancel_queued_behind_auth(void)
{
	struct fp5_cmdbuf b;
	struct fp5_attempt a;
	char out[FP5_CMD_LINE];

	memset(&b, 0, sizeof(b));
	fp5_cmd_push(&b, "auth\ncancel\n", 12);
	CHECK(fp5_cmd_next(&b, out, sizeof(out)) && fp5_cmd_parse(out) == FP5_CMD_AUTH, "auth");
	fp5_att_begin(&a, 1);
	CHECK(fp5_cmd_next(&b, out, sizeof(out)), "cancel still queued");
	fp5_att_cmd(&a, fp5_cmd_parse(out));
	CHECK(a.cancelled && !fp5_att_irq(&a, 2), "queued cancel applies to the new auth");
}

/* User report Oct 26: a held wrong finger was matched again and again
 * (arm query "already down" every ~0.7 s, 5 strikes, lockout). */
static void test_held_miss_is_one_attempt(void)
{
	CHECK(fp5_lift_after(FP5_ATT_MISS), "miss asks for a lift");
	CHECK(fp5_lift_after(FP5_ATT_AGAIN), "all-zero miss asks for a lift");
	CHECK(!fp5_lift_after(FP5_ATT_HIT), "hit does not");
	CHECK(!fp5_lift_after(FP5_ATT_CANCELLED), "cancel does not");
	CHECK(!fp5_lift_after(FP5_ATT_NONE), "no finger does not");
	CHECK(!fp5_lift_after(-1), "error does not");

	/* Normal arming is unchanged. */
	CHECK(fp5_lift_gate(0, 1, 1, 0) == FP5_GATE_PRESS, "already down, fresh");
	CHECK(fp5_lift_gate(0, 0, 1, 5) == FP5_GATE_PRESS, "irq down, fresh");
	CHECK(fp5_lift_gate(0, 0, 0, 5) == FP5_GATE_NONE, "idle");
	/* After a miss the re-arm query sees the same finger. */
	CHECK(fp5_lift_gate(1, 1, 1, 0) == FP5_GATE_HELD, "held after miss");
	CHECK(fp5_lift_gate(1, 1, 1, 60000) == FP5_GATE_HELD, "held for a minute");
	CHECK(fp5_lift_gate(1, 1, 0, 0) == FP5_GATE_NONE, "query: no finger");
	/* A late arm irq for that finger is still the same press. */
	CHECK(fp5_lift_gate(1, 0, 1, FP5_LIFT_EDGE_MS - 1) == FP5_GATE_HELD,
	      "edge right after re-arm");
	CHECK(fp5_lift_gate(1, 0, 1, FP5_LIFT_EDGE_MS) == FP5_GATE_PRESS,
	      "new edge later is a new touch");

	CHECK(fp5_lift_check_ms(0) == 200, "fast checks first");
	CHECK(fp5_lift_check_ms(4999) == 200, "for 5 s");
	CHECK(fp5_lift_check_ms(5000) == 500, "then 0.5 s");
	CHECK(fp5_lift_check_ms(60000) == 2000, "long hold (pocket) 2 s");
}

/* Shared attempt: the watcher's auth wins and is run next. */
static void test_share_watcher_wins(void)
{
	struct fp5_attempt a;
	int pending = 0, drop = 0;

	fp5_att_begin(&a, 0);
	CHECK(fp5_share_from_watcher(&a, FP5_CMD_CANCEL, &pending) == FP5_REPLY_NONE,
	      "watcher cancel accepted");
	CHECK(!a.cancelled && !pending, "watcher cancel does not touch a shared attempt");
	CHECK(fp5_share_from_watcher(&a, FP5_CMD_AUTH, &pending) == FP5_REPLY_NONE,
	      "watcher auth is not busy");
	CHECK(a.cancelled && pending, "watcher auth cancels the shared attempt and queues");
	CHECK(fp5_att_finish(&a, 1) == FP5_ATT_CANCELLED, "no hit for the client after that");
	CHECK(!drop, "client not dropped by the watcher");
	fp5_share_from_watcher(&a, FP5_CMD_CANCEL, &pending);
	CHECK(pending == 2, "a cancel after the pending auth is kept for it");
	fp5_share_from_watcher(&a, FP5_CMD_CANCEL, &pending);
	CHECK(pending == 2, "and stays so");
}

static void test_unshift(void)
{
	struct fp5_cmdbuf b;
	char line[FP5_CMD_LINE];

	memset(&b, 0, sizeof(b));
	fp5_cmd_push(&b, "auth\n", 5);
	CHECK(fp5_cmd_unshift(&b, "cancel"), "unshift fits");
	CHECK(fp5_cmd_next(&b, line, sizeof(line)) && !strcmp(line, "cancel"), "cancel first");
	CHECK(fp5_cmd_next(&b, line, sizeof(line)) && !strcmp(line, "auth"), "then the queued line");
	memset(&b, 0, sizeof(b));
	memset(line, 'x', sizeof(line));
	fp5_cmd_push(&b, line, sizeof(b.buf) - 2);
	CHECK(!fp5_cmd_unshift(&b, "cancel"), "no room: refused");
	CHECK(b.n == sizeof(b.buf) - 2, "and the buffer is unchanged");
}

static void test_share_watcher_quit(void)
{
	struct fp5_attempt a;
	int pending = 0;

	fp5_att_begin(&a, 0);
	fp5_share_from_watcher(&a, FP5_CMD_QUIT, &pending);
	CHECK(a.cancelled && a.quit && !pending, "watcher quit cancels and quits");
	fp5_att_begin(&a, 0);
	CHECK(fp5_share_from_watcher(&a, FP5_CMD_UNKNOWN, &pending) == FP5_REPLY_UNKNOWN,
	      "unknown from the watcher");
}

static void test_share_client(void)
{
	struct fp5_attempt a;
	int drop = 0;

	fp5_att_begin(&a, 0);
	CHECK(fp5_share_from_client(&a, FP5_CMD_AUTH, &drop) == FP5_REPLY_BUSY,
	      "second client auth is busy");
	CHECK(!a.cancelled, "and does not cancel");
	fp5_share_from_client(&a, FP5_CMD_CANCEL, &drop);
	CHECK(a.cancelled && !drop && !a.quit, "client cancel keeps the client and the session");
	fp5_att_begin(&a, 0);
	fp5_share_from_client(&a, FP5_CMD_QUIT, &drop);
	CHECK(a.cancelled && drop && !a.quit, "client quit/EOF drops only the client");
	CHECK(fp5_share_from_client(&a, FP5_CMD_UNKNOWN, &drop) == FP5_REPLY_UNKNOWN,
	      "unknown from the client");
}

static void test_share_pick(void)
{
	CHECK(fp5_share_pick(1, 1) == 1, "watcher first when both have a line");
	CHECK(fp5_share_pick(0, 1) == 2, "client when only it has one");
	CHECK(fp5_share_pick(1, 0) == 1, "watcher alone");
	CHECK(fp5_share_pick(0, 0) == 0, "nothing");
	CHECK(FP5_SHARE_FIRST > 1000, "shared numbers far from watcher numbers");
}

int main(void)
{
	test_share_watcher_wins();
	test_share_watcher_quit();
	test_share_client();
	test_share_pick();
	test_unshift();
	test_held_miss_is_one_attempt();
	test_lines();
	test_parse();
	test_overlong();
	test_small_out();
	test_cancel_after_down_never_hits();
	test_cancel_beats_pending_irq();
	test_touch_between_attempts_does_not_carry();
	test_commands_during_wait();
	test_cancel_queued_behind_auth();
	if (fails) {
		printf("%d failed\n", fails);
		return 1;
	}
	printf("ok\n");
	return 0;
}
