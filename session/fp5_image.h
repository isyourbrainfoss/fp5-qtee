#ifndef FP5_IMAGE_H
#define FP5_IMAGE_H

#include <stddef.h>
#include <stdint.h>

struct fp5_seg {
	const uint8_t *data;
	size_t len;
};

/*
 * Build one ELF image from a Qualcomm split trustlet.
 * mdt is the ELF header (and any bytes that sit between segments).
 * segs are the .b0N files in program-header order, one per phdr with
 * p_filesz > 0. Each segment is copied to that phdr's p_offset.
 * Bytes of mdt that no segment covers are kept (the hash gap).
 *
 * On success returns 0 and a malloc'd buffer in *out.
 * On failure returns a negative errno-style code and *out is NULL.
 */
int fp5_mbn_from_mdt(const uint8_t *mdt, size_t mdt_len,
		     const struct fp5_seg *segs, size_t nseg,
		     uint8_t **out, size_t *out_len);

#endif
