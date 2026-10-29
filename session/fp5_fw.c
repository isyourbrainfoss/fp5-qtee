/*
 * Trustlet file loading: a prebuilt .mbn first, the split .mdt/.bNN second.
 */
#include "fp5_fw.h"
#include "fp5_image.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define FP5_FW_MAX_FILE (64u << 20)

static int slurp(const char *path, uint8_t **out, size_t *len)
{
	struct stat st;
	uint8_t *buf;
	size_t got = 0;
	int fd;

	*out = NULL;
	*len = 0;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -errno;
	if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size <= 0 ||
	    (unsigned long long)st.st_size > FP5_FW_MAX_FILE) {
		close(fd);
		return -EINVAL;
	}
	buf = malloc((size_t)st.st_size);
	if (!buf) {
		close(fd);
		return -ENOMEM;
	}
	while (got < (size_t)st.st_size) {
		ssize_t n = read(fd, buf + got, (size_t)st.st_size - got);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0) {
			free(buf);
			close(fd);
			return n < 0 ? -errno : -EIO;
		}
		got += (size_t)n;
	}
	close(fd);
	*out = buf;
	*len = got;
	return 0;
}

int fp5_fw_is_elf(const uint8_t *buf, size_t len)
{
	return buf && len >= 52 && buf[0] == 0x7f && buf[1] == 'E' &&
	       buf[2] == 'L' && buf[3] == 'F';
}

const char *fp5_fw_dir(const char *arg)
{
	const char *env = getenv("FP5_QTEE_FW_DIR");

	if (env && *env)
		return env;
	if (arg && *arg)
		return arg;
	return FP5_FW_DEFAULT_DIR;
}

int fp5_fw_load_split(const char *dir, const char *name, uint8_t **out,
		      size_t *out_len)
{
	char path[512];
	uint8_t *mdt = NULL, *raw[FP5_FW_MAX_SEGS] = { 0 };
	struct fp5_seg segs[FP5_FW_MAX_SEGS];
	size_t mdt_len = 0;
	size_t n = 0;
	int rc, i;

	snprintf(path, sizeof(path), "%s/%s.mdt", dir, name);
	rc = slurp(path, &mdt, &mdt_len);
	if (rc)
		return rc;
	for (i = 0; i < FP5_FW_MAX_SEGS; i++) {
		size_t len = 0;

		snprintf(path, sizeof(path), "%s/%s.b%02d", dir, name, i);
		if (access(path, F_OK))
			break;
		rc = slurp(path, &raw[i], &len);
		if (rc)
			goto out;
		segs[i].data = raw[i];
		segs[i].len = len;
		n++;
	}
	rc = n ? fp5_mbn_from_mdt(mdt, mdt_len, segs, n, out, out_len)
	       : -ENOENT;
out:
	free(mdt);
	for (i = 0; i < FP5_FW_MAX_SEGS; i++)
		free(raw[i]);
	return rc;
}

static int load_from(const char *dir, const char *name, uint8_t **out,
		     size_t *out_len, char *used, size_t used_len)
{
	char path[512];
	int rc;

	snprintf(path, sizeof(path), "%s/%s.mbn", dir, name);
	rc = slurp(path, out, out_len);
	if (rc == 0) {
		if (fp5_fw_is_elf(*out, *out_len)) {
			if (used)
				snprintf(used, used_len, "%s", path);
			return 0;
		}
		free(*out);
		*out = NULL;
		*out_len = 0;
		rc = -EINVAL;	/* not an ELF: try the split files */
	}
	rc = fp5_fw_load_split(dir, name, out, out_len);
	if (rc == 0 && used)
		snprintf(used, used_len, "%s/%s.mdt", dir, name);
	return rc;
}

int fp5_fw_load(const char *dir, const char *name, uint8_t **out,
		size_t *out_len, char *used, size_t used_len)
{
	int rc;

	*out = NULL;
	*out_len = 0;
	if (!dir || !name || !*name || strchr(name, '/'))
		return -EINVAL;
	rc = load_from(dir, name, out, out_len, used, used_len);
	if (rc && !strcmp(dir, FP5_FW_DEFAULT_DIR))
		rc = load_from(FP5_FW_LEGACY_DIR, name, out, out_len, used,
			       used_len);
	return rc;
}
