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

int main(void)
{
	test_lines();
	test_parse();
	test_overlong();
	test_small_out();
	if (fails) {
		printf("%d failed\n", fails);
		return 1;
	}
	printf("ok\n");
	return 0;
}
