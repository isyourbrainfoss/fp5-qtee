#include "fp5_finger.h"

#include <stdio.h>
#include <string.h>

static int fails;

#define CHECK(c, msg) do { if (!(c)) { printf("FAIL %s\n", msg); fails++; } } while (0)

static void put32(uint8_t *p, uint32_t v)
{
	memcpy(p, &v, 4);
}

static uint32_t get32(const uint8_t *p)
{
	uint32_t v;

	memcpy(&v, p, 4);
	return v;
}

static void test_opcodes(void)
{
	/* fingerprint.default.so table at 0x97a08 / 0x97a10. */
	CHECK(FP5_OP_LIST == 0x2005 && FP5_LIST_PLEN == 4, "enumerate is {0x2005, 4}");
	CHECK(FP5_OP_REMOVE == 0x2006 && FP5_REMOVE_PLEN == 8, "remove is {0x2006, 8}");
	CHECK(FP5_OP_LIST != 0x1028, "0x1028 is not enumerate");
}

static void test_build_list(void)
{
	uint8_t pay[64];

	memset(pay, 0xee, sizeof(pay));
	fp5_build_list(pay, sizeof(pay), FP5_LIST_MAX);
	CHECK(get32(pay) == FP5_LIST_MAX, "capacity word");
	CHECK(get32(pay + 8) == 0 && get32(pay + 40) == 0, "stale entries cleared");
	CHECK(pay[FP5_LIST_REPLY] == 0xee, "clears only the reply area");
}

static void test_parse_two(void)
{
	uint8_t pay[64] = { 0 };
	struct fp5_finger f[FP5_LIST_MAX];

	/* Two fingers, as the HAL copies them: {gid, fid} at +4, stride 8. */
	put32(pay, 2);
	put32(pay + 4, 0);
	put32(pay + 8, 1558157930u);
	put32(pay + 12, 0);
	put32(pay + 16, 1403494260u);
	CHECK(fp5_parse_list(pay, sizeof(pay), 2, 5, f, 5) == 2, "two fingers");
	CHECK(f[0].fid == 1558157930u && f[0].gid == 0, "first fid at +8");
	CHECK(f[1].fid == 1403494260u, "second fid at +16, not +12");
}

static void test_parse_reject(void)
{
	uint8_t pay[64] = { 0 };
	struct fp5_finger f[FP5_LIST_MAX];

	CHECK(fp5_parse_list(pay, sizeof(pay), 0, 5, f, 5) == 0, "empty gallery");
	CHECK(fp5_parse_list(pay, sizeof(pay), -203, 5, f, 5) == -1, "-203 is not a list");
	/* The TA left the capacity word alone: it did not answer. */
	put32(pay, 5);
	CHECK(fp5_parse_list(pay, sizeof(pay), 0, 5, f, 5) == -1, "untouched capacity");
	put32(pay, 1);
	put32(pay + 8, 7);
	CHECK(fp5_parse_list(pay, sizeof(pay), 2, 5, f, 5) == -1, "status and count disagree");
	put32(pay, 6);
	CHECK(fp5_parse_list(pay, sizeof(pay), 6, 5, f, 5) == -1, "above capacity");
	put32(pay, 1);
	put32(pay + 8, 0);
	CHECK(fp5_parse_list(pay, sizeof(pay), 1, 5, f, 5) == -1, "fid 0 is not a finger");
	put32(pay, 2);
	put32(pay + 8, 9);
	put32(pay + 16, 10);
	CHECK(fp5_parse_list(pay, 12, 2, 5, f, 5) == -1, "short buffer");
	CHECK(fp5_parse_list(pay, 20, 2, 5, f, 5) == 2, "exact buffer");
	CHECK(fp5_parse_list(NULL, 20, 2, 5, f, 5) == -1, "null");
}

static void test_build_remove(void)
{
	uint8_t pay[16];

	memset(pay, 0xee, sizeof(pay));
	fp5_build_remove(pay, sizeof(pay), 0, 1558157930u);
	CHECK(get32(pay) == 0, "gid first");
	CHECK(get32(pay + 4) == 1558157930u, "fid second (fid alone answers -200)");
	CHECK(pay[8] == 0xee, "8 bytes only");
	memset(pay, 0xee, sizeof(pay));
	fp5_build_remove(pay, 4, 0, 1);
	CHECK(pay[0] == 0xee, "short buffer untouched");
}

static void test_args(void)
{
	uint32_t fid = 0;
	char *ok[] = { "1558157930", "--yes-really" };
	char *ok2[] = { "--yes-really", "1558157930" };
	char *noyes[] = { "1558157930" };
	char *yesonly[] = { "--yes-really" };
	char *two[] = { "1", "2", "--yes-really" };
	char *bad[] = { "0x5ce0", "--yes-really" };
	char *neg[] = { "-5", "--yes-really" };
	char *zero[] = { "0", "--yes-really" };
	char *big[] = { "4294967296", "--yes-really" };
	char *typo[] = { "1558157930", "--yes" };
	char *last[] = { "1558157930", "--yes-really", "--allow-last" };

	CHECK(fp5_remove_args(2, ok, &fid) == FP5_RM_OK && fid == 1558157930u, "fid then flag");
	fid = 0;
	CHECK(fp5_remove_args(2, ok2, &fid) == FP5_RM_OK && fid == 1558157930u, "flag then fid");
	CHECK(fp5_remove_args(1, noyes, &fid) == FP5_RM_NO_YES, "no --yes-really");
	CHECK(fp5_remove_args(0, NULL, &fid) == FP5_RM_NO_FID, "nothing");
	CHECK(fp5_remove_args(1, yesonly, &fid) == FP5_RM_NO_FID, "flag alone");
	CHECK(fp5_remove_args(3, two, &fid) == FP5_RM_EXTRA, "two ids");
	CHECK(fp5_remove_args(2, bad, &fid) == FP5_RM_NO_FID, "hex refused");
	CHECK(fp5_remove_args(2, neg, &fid) == FP5_RM_EXTRA, "negative is a flag");
	CHECK(fp5_remove_args(2, zero, &fid) == FP5_RM_NO_FID, "zero");
	CHECK(fp5_remove_args(2, big, &fid) == FP5_RM_NO_FID, "above u32");
	CHECK(fp5_remove_args(2, typo, &fid) == FP5_RM_EXTRA, "--yes is not --yes-really");
	CHECK(fp5_remove_args(3, last, &fid) == FP5_RM_OK, "--allow-last accepted");
}

static void test_check(void)
{
	struct fp5_finger two[2] = { { 0, 1558157930u }, { 0, 1403494260u } };
	struct fp5_finger one[1] = { { 0, 1558157930u } };
	uint32_t gid = 99;

	CHECK(fp5_remove_check(two, 2, 1403494260u, 0, &gid) == FP5_RM_OK && gid == 0,
	      "listed finger, gid from list");
	CHECK(fp5_remove_check(two, 2, 42, 0, &gid) == FP5_RM_NOT_LISTED, "not listed");
	CHECK(fp5_remove_check(two, 0, 1558157930u, 0, &gid) == FP5_RM_NOT_LISTED, "empty list");
	CHECK(fp5_remove_check(one, 1, 1558157930u, 0, &gid) == FP5_RM_LAST, "last finger refused");
	CHECK(fp5_remove_check(one, 1, 1558157930u, 1, &gid) == FP5_RM_OK, "--allow-last");
	CHECK(fp5_list_lacks(one, 1, 1403494260u), "lacks");
	CHECK(!fp5_list_lacks(two, 2, 1403494260u), "still there");
	CHECK(fp5_list_lacks(two, 0, 1403494260u), "empty lacks all");
	CHECK(strcmp(fp5_remove_reason(FP5_RM_NO_YES), "refusing without --yes-really") == 0,
	      "reason text");
}

int main(void)
{
	test_opcodes();
	test_build_list();
	test_parse_two();
	test_parse_reject();
	test_build_remove();
	test_args();
	test_check();
	if (fails) {
		printf("%d failures\n", fails);
		return 1;
	}
	printf("test_fp5_finger: ok\n");
	return 0;
}
