#include "fp5_finger.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>

static uint32_t rd32(const uint8_t *p)
{
	uint32_t v;

	memcpy(&v, p, 4);
	return v;
}

static void wr32(uint8_t *p, uint32_t v)
{
	memcpy(p, &v, 4);
}

void fp5_build_list(uint8_t *pay, size_t cap, uint32_t capacity)
{
	if (!pay || cap < 4)
		return;
	memset(pay, 0, cap < FP5_LIST_REPLY ? cap : FP5_LIST_REPLY);
	wr32(pay, capacity);
}

int fp5_parse_list(const uint8_t *pay, size_t n, int32_t status,
		   uint32_t capacity, struct fp5_finger *out, int max)
{
	uint32_t count, word0;
	uint32_t i;

	if (!pay || !out || max < 0 || n < 4 || status < 0)
		return -1;
	count = (uint32_t)status;
	word0 = rd32(pay);
	/* A positive status is the count, and the payload repeats it. A
	 * zero status with a zero word is an empty gallery. Anything else
	 * is not an answer this decoder understands. */
	if (word0 != count)
		return -1;
	if (count > capacity || count > (uint32_t)max)
		return -1;
	if (4u + 8u * count > n)
		return -1;
	for (i = 0; i < count; i++) {
		out[i].gid = rd32(pay + 4 + 8 * i);
		out[i].fid = rd32(pay + 8 + 8 * i);
		if (out[i].fid == 0)
			return -1;
	}
	return (int)count;
}

void fp5_build_remove(uint8_t *pay, size_t cap, uint32_t gid, uint32_t fid)
{
	if (!pay || cap < FP5_REMOVE_PLEN)
		return;
	memset(pay, 0, FP5_REMOVE_PLEN);
	wr32(pay, gid);
	wr32(pay + 4, fid);
}

int fp5_parse_fid(const char *s, uint32_t *fid)
{
	char *end;
	unsigned long long v;
	const char *p;

	if (!s || !*s || !fid)
		return -1;
	for (p = s; *p; p++) {
		if (*p < '0' || *p > '9')
			return -1;
	}
	errno = 0;
	v = strtoull(s, &end, 10);
	if (errno || *end || v == 0 || v > 0xffffffffull)
		return -1;
	*fid = (uint32_t)v;
	return 0;
}

int fp5_remove_args(int argc, char *const *argv, uint32_t *fid)
{
	int i, yes = 0, have = 0;
	uint32_t v = 0;

	for (i = 0; i < argc; i++) {
		const char *a = argv[i];

		if (!a)
			continue;
		if (!strcmp(a, "--yes-really")) {
			yes = 1;
		} else if (!strcmp(a, "--allow-last")) {
			/* checked by the caller with fp5_remove_check */
		} else if (a[0] == '-') {
			return FP5_RM_EXTRA;
		} else {
			if (have)
				return FP5_RM_EXTRA;
			if (fp5_parse_fid(a, &v))
				return FP5_RM_NO_FID;
			have = 1;
		}
	}
	if (!have)
		return FP5_RM_NO_FID;
	if (!yes)
		return FP5_RM_NO_YES;
	if (fid)
		*fid = v;
	return FP5_RM_OK;
}

int fp5_remove_check(const struct fp5_finger *list, int n, uint32_t fid,
		     int allow_last, uint32_t *gid)
{
	int i;

	if (!list || n <= 0 || fid == 0)
		return FP5_RM_NOT_LISTED;
	for (i = 0; i < n; i++) {
		if (list[i].fid != fid)
			continue;
		if (n == 1 && !allow_last)
			return FP5_RM_LAST;
		if (gid)
			*gid = list[i].gid;
		return FP5_RM_OK;
	}
	return FP5_RM_NOT_LISTED;
}

const char *fp5_remove_reason(int rc)
{
	switch (rc) {
	case FP5_RM_OK: return "ok";
	case FP5_RM_NO_FID: return "need one finger id (decimal, as list prints it)";
	case FP5_RM_NO_YES: return "refusing without --yes-really";
	case FP5_RM_EXTRA: return "unexpected argument";
	case FP5_RM_NOT_LISTED: return "finger id is not in the trustlet's list";
	case FP5_RM_LAST: return "it is the only enrolled finger (add --allow-last)";
	}
	return "unknown";
}

int fp5_list_lacks(const struct fp5_finger *list, int n, uint32_t fid)
{
	int i;

	for (i = 0; i < n; i++) {
		if (list[i].fid == fid)
			return 0;
	}
	return 1;
}
