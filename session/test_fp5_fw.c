#include "fp5_fw.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void fail(const char *msg)
{
	fprintf(stderr, "FAIL %s\n", msg);
	exit(1);
}

static void put(const char *dir, const char *file, const void *b, size_t n)
{
	char p[512];
	FILE *f;

	snprintf(p, sizeof(p), "%s/%s", dir, file);
	f = fopen(p, "wb");
	if (!f || fwrite(b, 1, n, f) != n)
		fail("write fixture");
	fclose(f);
}

/* Same ELF32 layout as test_fp5_image.c: two segments, a gap between. */
static void split_fixture(const char *dir, uint8_t *mdt)
{
	uint8_t s0[4] = { 0x11, 0x22, 0x33, 0x44 };
	uint8_t s1[4] = { 0xaa, 0xbb, 0xcc, 0xdd };

	memset(mdt, 0, 128);
	mdt[0] = 0x7f; mdt[1] = 'E'; mdt[2] = 'L'; mdt[3] = 'F';
	mdt[4] = 1; mdt[5] = 1;
	mdt[28] = 52; mdt[42] = 32; mdt[44] = 2;
	mdt[52 + 4] = 4; mdt[52 + 16] = 4;
	mdt[84 + 4] = 64; mdt[84 + 16] = 4;
	put(dir, "t.mdt", mdt, 128);
	put(dir, "t.b00", s0, 4);
	put(dir, "t.b01", s1, 4);
}

int main(void)
{
	char tmpl[] = "/tmp/fp5fwXXXXXX";
	char *dir = mkdtemp(tmpl);
	uint8_t mdt[128], *img = NULL, *img2 = NULL;
	size_t len = 0, len2 = 0;
	char used[512];

	if (!dir)
		fail("mkdtemp");
	unsetenv("FP5_QTEE_FW_DIR");
	if (strcmp(fp5_fw_dir(NULL), FP5_FW_DEFAULT_DIR))
		fail("default dir");
	if (strcmp(fp5_fw_dir("/x"), "/x"))
		fail("arg dir");
	setenv("FP5_QTEE_FW_DIR", "/y", 1);
	if (strcmp(fp5_fw_dir("/x"), "/y"))
		fail("env dir wins");
	unsetenv("FP5_QTEE_FW_DIR");

	/* 1. split only: assembled in memory */
	split_fixture(dir, mdt);
	if (fp5_fw_load(dir, "t", &img, &len, used, sizeof(used)))
		fail("split load");
	if (!strstr(used, "t.mdt") || len < 68 || img[4] != 0x11 ||
	    img[64] != 0xaa)
		fail("split content");

	/* 2. a prebuilt .mbn is preferred, byte for byte */
	img[100] = 0x5a;	/* mark it so we can tell it apart */
	put(dir, "t.mbn", img, len);
	if (fp5_fw_load(dir, "t", &img2, &len2, used, sizeof(used)))
		fail("mbn load");
	if (!strstr(used, "t.mbn") || len2 != len || memcmp(img, img2, len))
		fail("mbn content");
	free(img2);

	/* 3. a .mbn that is not ELF falls back to the split files */
	put(dir, "t.mbn", "garbage-not-elf-garbage-not-elf-garbage-not-elf-garbage", 56);
	if (fp5_fw_load(dir, "t", &img2, &len2, used, sizeof(used)) ||
	    !strstr(used, "t.mdt"))
		fail("non-elf fallback");
	free(img2);

	/* 4. nothing there */
	if (fp5_fw_load(dir, "missing", &img2, &len2, NULL, 0) == 0)
		fail("missing must fail");
	if (fp5_fw_load(dir, "../t", &img2, &len2, NULL, 0) == 0)
		fail("path in name must fail");

	free(img);
	printf("test_fp5_fw ok\n");
	return 0;
}
