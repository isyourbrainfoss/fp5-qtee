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
