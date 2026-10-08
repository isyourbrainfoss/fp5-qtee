#include "fp5_image.h"
#include "fp5_tee.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int read_file(const char *path, uint8_t **out, size_t *len)
{
	FILE *f;
	long sz;
	uint8_t *b;
	size_t n;

	f = fopen(path, "rb");
	if (!f) {
		fprintf(stderr, "open %s: %s\n", path, strerror(errno));
		return -1;
	}
	if (fseek(f, 0, SEEK_END) != 0) {
		fclose(f);
		return -1;
	}
	sz = ftell(f);
	if (sz <= 0 || sz > 8 * 1024 * 1024) {
		fclose(f);
		fprintf(stderr, "bad size %s %ld\n", path, sz);
		return -1;
	}
	rewind(f);
	b = malloc((size_t)sz);
	if (!b) {
		fclose(f);
		return -1;
	}
	n = fread(b, 1, (size_t)sz, f);
	fclose(f);
	if (n != (size_t)sz) {
		free(b);
		fprintf(stderr, "short read %s\n", path);
		return -1;
	}
	*out = b;
	*len = (size_t)sz;
	return 0;
}

static const char *verdict(uint32_t r)
{
	switch (r) {
	case 0: return "success";
	case 11: return "PIL_ROLLBACK";
	case 12: return "ELF_SIGNATURE";
	case 13: return "METADATA_INVALID";
	case 16: return "ALREADY_LOADED";
	case 28: return "ELF_LOADING";
	default: return "";
	}
}

int main(int argc, char **argv)
{
	const char *dir = argc > 1 ? argv[1] : "/lib/firmware/qsee";
	const char *name = "focal32";
	char path[256];
	uint8_t *mdt = NULL, *img = NULL;
	size_t mdt_len = 0, img_len = 0;
	struct fp5_seg segs[8];
	uint8_t *raw[8] = { 0 };
	size_t raw_len[8] = { 0 };
	struct fp5_tee tee = { .fd = -1 };
	uint64_t env = 0, loader = 0, app = 0;
	uint32_t qret = 0;
	char dist[128];
	int i, rc, ret = 1;

	snprintf(path, sizeof(path), "%s/%s.mdt", dir, name);
	if (read_file(path, &mdt, &mdt_len))
		goto out;
	for (i = 0; i < 8; i++) {
		snprintf(path, sizeof(path), "%s/%s.b0%d", dir, name, i);
		if (read_file(path, &raw[i], &raw_len[i]))
			goto out;
		segs[i].data = raw[i];
		segs[i].len = raw_len[i];
	}
	rc = fp5_mbn_from_mdt(mdt, mdt_len, segs, 8, &img, &img_len);
	if (rc) {
		fprintf(stderr, "mbn assemble rc=%d\n", rc);
		goto out;
	}
	printf("image bytes=%zu mdt=%zu\n", img_len, mdt_len);

	if (fp5_tee_open(&tee, "/dev/tee0")) {
		fprintf(stderr, "open /dev/tee0: %s\n", strerror(errno));
		goto out;
	}
	rc = fp5_tee_client_env(&tee, &env, &qret);
	printf("client_env ioctl_rc=%d qret=%u env=%llu\n", rc, qret,
	       (unsigned long long)env);
	if (rc)
		goto out;
	rc = fp5_tee_open_service(&tee, env, 122, &loader, &qret);
	printf("open_loader ioctl_rc=%d qret=%u loader=%llu\n", rc, qret,
	       (unsigned long long)loader);
	if (rc)
		goto out;
	memset(dist, 0, sizeof(dist));
	rc = fp5_tee_load_buffer(&tee, loader, img, img_len, name, strlen(name),
				 dist, sizeof(dist), &app, &qret);
	printf("loadFromBuffer ioctl_rc=%d qret=%u %s dist='%s' app=%llu errno=%d\n",
	       rc, qret, verdict(qret), dist, (unsigned long long)app, errno);
	ret = (rc == 0 && qret == 0) ? 0 : 2;
out:
	/* One exit path, so a failed segment read does not leak the others. */
	fp5_tee_close(&tee);
	free(img);
	free(mdt);
	for (i = 0; i < 8; i++)
		free(raw[i]);
	return ret;
}
