#ifndef FP5_FW_H
#define FP5_FW_H

#include <stddef.h>
#include <stdint.h>

/* Default trustlet directory: droid-juicer (BengalOS) writes here. */
#define FP5_FW_DEFAULT_DIR "/var/lib/firmware/qsee"
/* Old location (Nura, hand-copied split files). Tried after the default. */
#define FP5_FW_LEGACY_DIR "/lib/firmware/qsee"
#define FP5_FW_MAX_SEGS 16

/*
 * Directory to load from: $FP5_QTEE_FW_DIR if set and non-empty, else
 * arg if non-NULL and non-empty, else FP5_FW_DEFAULT_DIR.
 */
const char *fp5_fw_dir(const char *arg);

/*
 * Load trustlet <name> as one ELF image.
 * Order: <dir>/<name>.mbn (prebuilt, e.g. squashed by droid-juicer), then
 * <dir>/<name>.mdt + .b00.. (assembled in memory). When dir is the default
 * and neither is there, FP5_FW_LEGACY_DIR is tried the same way.
 * On success returns 0, a malloc'd image in *out, and the path used in
 * used (if used != NULL, at most used_len bytes). Negative on failure.
 */
int fp5_fw_load(const char *dir, const char *name, uint8_t **out,
		size_t *out_len, char *used, size_t used_len);

/* Only the split files: <dir>/<name>.mdt + .b00.. (no .mbn, no fallback). */
int fp5_fw_load_split(const char *dir, const char *name, uint8_t **out,
		      size_t *out_len);

/* 1 if buf looks like an ELF image (magic only). */
int fp5_fw_is_elf(const uint8_t *buf, size_t len);

#endif
