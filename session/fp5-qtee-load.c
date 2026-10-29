#include "fp5_fw.h"
#include "fp5_image.h"
#include "fp5_tee.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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

/*
 * fp5-qtee-load [DIR]                 load focal32 into QTEE (needs /dev/tee0)
 * fp5-qtee-load --assemble DIR OUT    only build the image from DIR's
 *                                     .mdt+.bNN and write it to OUT (no TEE),
 *                                     for a byte compare with droid-juicer's
 *                                     focal32.mbn.
 */
static int assemble_only(const char *dir, const char *outpath)
{
	char split[512];
	uint8_t *img = NULL;
	size_t img_len = 0;
	char used[512];
	FILE *f;
	int rc;

	/* Force the split files: a .mbn next to them must not short-cut this. */
	snprintf(split, sizeof(split), "%s", dir);
	rc = fp5_fw_load_split(split, "focal32", &img, &img_len);
	if (rc) {
		fprintf(stderr, "assemble from %s/focal32.mdt failed rc=%d\n", dir, rc);
		return 1;
	}
	snprintf(used, sizeof(used), "%s/focal32.mdt", dir);
	f = fopen(outpath, "wbx");
	if (!f) {
		fprintf(stderr, "create %s: %s\n", outpath, strerror(errno));
		free(img);
		return 1;
	}
	rc = fwrite(img, 1, img_len, f) == img_len ? 0 : 1;
	if (fclose(f))
		rc = 1;
	printf("wrote %zu bytes from %s to %s\n", img_len, used, outpath);
	free(img);
	return rc;
}

int main(int argc, char **argv)
{
	const char *dir;
	const char *name = "focal32";
	uint8_t *img = NULL;
	size_t img_len = 0;
	char used[512];
	struct fp5_tee tee = { .fd = -1 };
	uint64_t env = 0, loader = 0, app = 0;
	uint32_t qret = 0;
	char dist[128];
	int rc, ret = 1;

	if (argc == 4 && !strcmp(argv[1], "--assemble"))
		return assemble_only(argv[2], argv[3]);
	dir = fp5_fw_dir(argc > 1 ? argv[1] : NULL);
	rc = fp5_fw_load(dir, name, &img, &img_len, used, sizeof(used));
	if (rc) {
		fprintf(stderr, "focal32 not found in %s (.mbn or .mdt+.bNN) rc=%d\n",
			dir, rc);
		goto out;
	}
	printf("image bytes=%zu from %s\n", img_len, used);

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
	fp5_tee_close(&tee);
	free(img);
	return ret;
}
