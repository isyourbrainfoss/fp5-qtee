#include "fp5_cmd.h"

#include <string.h>

void fp5_cmd_push(struct fp5_cmdbuf *b, const char *data, size_t len)
{
	size_t i;

	for (i = 0; i < len; i++) {
		char c = data[i];

		if (b->skipping) {
			if (c == '\n')
				b->skipping = 0;
			continue;
		}
		if (b->n >= sizeof(b->buf)) {
			/* Over-long line. Drop it, and the rest of it. */
			b->n = 0;
			b->skipping = c != '\n';
			continue;
		}
		b->buf[b->n++] = c;
	}
}

int fp5_cmd_next(struct fp5_cmdbuf *b, char *out, size_t cap)
{
	char *nl;
	size_t len;

	if (!cap)
		return 0;
	nl = memchr(b->buf, '\n', b->n);
	if (!nl)
		return 0;
	len = (size_t)(nl - b->buf);
	if (len >= cap)
		len = cap - 1;
	memcpy(out, b->buf, len);
	out[len] = 0;
	len = (size_t)(nl - b->buf) + 1;
	memmove(b->buf, b->buf + len, b->n - len);
	b->n -= len;
	return 1;
}

int fp5_cmd_parse(const char *line)
{
	size_t n = strlen(line);

	while (n && (line[n - 1] == '\r' || line[n - 1] == ' ' || line[n - 1] == '\t'))
		n--;
	if (n == 4 && !memcmp(line, "auth", 4))
		return FP5_CMD_AUTH;
	if (n == 6 && !memcmp(line, "cancel", 6))
		return FP5_CMD_CANCEL;
	if (n == 4 && !memcmp(line, "quit", 4))
		return FP5_CMD_QUIT;
	if (n == 0)
		return FP5_CMD_NONE;
	return FP5_CMD_UNKNOWN;
}

void fp5_att_begin(struct fp5_attempt *a, unsigned irq_now)
{
	a->irq = irq_now;
	a->down = 0;
	a->cancelled = 0;
	a->quit = 0;
}

int fp5_att_cmd(struct fp5_attempt *a, int cmd)
{
	switch (cmd) {
	case FP5_CMD_QUIT:
		a->quit = 1;
		a->cancelled = 1;
		return FP5_REPLY_NONE;
	case FP5_CMD_CANCEL:
		a->cancelled = 1;
		return FP5_REPLY_NONE;
	case FP5_CMD_AUTH:
		return FP5_REPLY_BUSY;
	case FP5_CMD_NONE:
		return FP5_REPLY_NONE;
	default:
		return FP5_REPLY_UNKNOWN;
	}
}

int fp5_att_irq(struct fp5_attempt *a, unsigned irq_now)
{
	if (a->cancelled)
		return 0;
	if (irq_now == a->irq)
		return 0;
	a->irq = irq_now;
	return 1;
}

int fp5_att_down(struct fp5_attempt *a)
{
	if (a->cancelled)
		return 0;
	a->down = 1;
	return 1;
}

int fp5_att_finish(const struct fp5_attempt *a, int matched)
{
	if (matched)
		return a->cancelled ? FP5_ATT_CANCELLED : FP5_ATT_HIT;
	/* A scored non-match is still a real press. Report it, so the
	 * watcher counts the strike even if it cancelled meanwhile. */
	return FP5_ATT_MISS;
}

int fp5_lift_after(int result)
{
	/* Scored no-match (stay or not), empty, too fast. Not a hit,
	 * not a cancel, not "no finger", not an error. */
	return result == FP5_ATT_MISS || result == FP5_ATT_AGAIN;
}

int fp5_lift_gate(int need_lift, int from_query, int real_down,
		  long ms_since_arm)
{
	if (!real_down)
		return FP5_GATE_NONE;
	if (!need_lift)
		return FP5_GATE_PRESS;
	if (from_query)
		return FP5_GATE_HELD;
	return ms_since_arm >= FP5_LIFT_EDGE_MS ? FP5_GATE_PRESS
						: FP5_GATE_HELD;
}

int fp5_lift_check_ms(long held_ms)
{
	if (held_ms < 5000)
		return 200;
	if (held_ms < 20000)
		return 500;
	return 2000;
}

int fp5_share_from_watcher(struct fp5_attempt *a, int cmd, int *pending_auth)
{
	switch (cmd) {
	case FP5_CMD_AUTH:
		/* The lock screen wants the sensor: it wins. */
		a->cancelled = 1;
		if (pending_auth)
			*pending_auth = 1;
		return FP5_REPLY_NONE;
	case FP5_CMD_QUIT:
		a->quit = 1;
		a->cancelled = 1;
		return FP5_REPLY_NONE;
	case FP5_CMD_CANCEL:
		if (pending_auth && *pending_auth == 1)
			*pending_auth = 2;
		return FP5_REPLY_NONE;
	case FP5_CMD_NONE:
		return FP5_REPLY_NONE;
	default:
		return FP5_REPLY_UNKNOWN;
	}
}

int fp5_cmd_unshift(struct fp5_cmdbuf *b, const char *line)
{
	size_t len = strlen(line) + 1;

	if (b->skipping || b->n + len > sizeof(b->buf))
		return 0;
	memmove(b->buf + len, b->buf, b->n);
	memcpy(b->buf, line, len - 1);
	b->buf[len - 1] = '\n';
	b->n += len;
	return 1;
}

int fp5_share_from_client(struct fp5_attempt *a, int cmd, int *drop_client)
{
	switch (cmd) {
	case FP5_CMD_QUIT:
		a->cancelled = 1;
		if (drop_client)
			*drop_client = 1;
		return FP5_REPLY_NONE;
	case FP5_CMD_CANCEL:
		a->cancelled = 1;
		return FP5_REPLY_NONE;
	case FP5_CMD_AUTH:
		return FP5_REPLY_BUSY;
	case FP5_CMD_NONE:
		return FP5_REPLY_NONE;
	default:
		return FP5_REPLY_UNKNOWN;
	}
}

int fp5_share_pick(int watcher_has_line, int client_has_line)
{
	if (watcher_has_line)
		return 1;
	if (client_has_line)
		return 2;
	return 0;
}
