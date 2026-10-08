#include "fp5_image.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void fail(const char *msg)
{
	fprintf(stderr, "FAIL %s\n", msg);
	exit(1);
}

/* ELF32, two phdrs. A gap between them must survive from the mdt. */
static void test_places_segments_and_keeps_gap(void)
{
	uint8_t mdt[128];
	uint8_t seg0[4] = { 0x11, 0x22, 0x33, 0x44 };
	uint8_t seg1[4] = { 0xaa, 0xbb, 0xcc, 0xdd };
	struct fp5_seg segs[2];
	uint8_t *out = NULL;
	size_t out_len = 0;
	int rc;

	memset(mdt, 0, sizeof(mdt));
	mdt[0] = 0x7f;
	mdt[1] = 'E';
	mdt[2] = 'L';
	mdt[3] = 'F';
	mdt[4] = 1;
	mdt[5] = 1;
	/* e_phoff = 52, e_phentsize = 32, e_phnum = 2 */
	mdt[28] = 52;
	mdt[42] = 32;
	mdt[44] = 2;
	/* ph0 at file offset 4, filesz 4 */
	mdt[52 + 4] = 4;
	mdt[52 + 16] = 4;
	/* ph1 at file offset 64, filesz 4 */
	mdt[84 + 4] = 64;
	mdt[84 + 16] = 4;
	/* gap marker that no segment covers */
	mdt[16] = 0x5a;
	mdt[17] = 0xa5;

	segs[0].data = seg0;
	segs[0].len = 4;
	segs[1].data = seg1;
	segs[1].len = 4;
	rc = fp5_mbn_from_mdt(mdt, sizeof(mdt), segs, 2, &out, &out_len);
	if (rc)
		fail("assemble");
	if (out_len != sizeof(mdt))
		fail("length");
	if (memcmp(out + 4, seg0, 4) != 0)
		fail("seg0");
	if (memcmp(out + 64, seg1, 4) != 0)
		fail("seg1");
	if (out[16] != 0x5a || out[17] != 0xa5)
		fail("gap");
	free(out);
}

static void test_rejects_bad_segment_size(void)
{
	uint8_t mdt[84];
	uint8_t seg[3] = { 1, 2, 3 };
	struct fp5_seg s = { seg, 3 };
	uint8_t *out = (uint8_t *)1;
	size_t out_len = 9;
	int rc;

	memset(mdt, 0, sizeof(mdt));
	mdt[0] = 0x7f;
	mdt[1] = 'E';
	mdt[2] = 'L';
	mdt[3] = 'F';
	mdt[4] = 1;
	mdt[5] = 1;
	mdt[28] = 52;
	mdt[42] = 32;
	mdt[44] = 1;
	mdt[52 + 16] = 4;
	rc = fp5_mbn_from_mdt(mdt, sizeof(mdt), &s, 1, &out, &out_len);
	if (rc == 0 || out != NULL || out_len != 0)
		fail("bad size should fail");
}

int main(void)
{
	test_places_segments_and_keeps_gap();
	test_rejects_bad_segment_size();
	printf("ok\n");
	return 0;
}
