#include "fp5_image.h"

#include <stdlib.h>
#include <string.h>

struct elf32_phdr {
	uint32_t type;
	uint32_t offset;
	uint32_t vaddr;
	uint32_t paddr;
	uint32_t filesz;
	uint32_t memsz;
	uint32_t flags;
	uint32_t align;
};

static uint32_t ru32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
	       ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint16_t ru16(const uint8_t *p)
{
	return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

int fp5_mbn_from_mdt(const uint8_t *mdt, size_t mdt_len,
		     const struct fp5_seg *segs, size_t nseg,
		     uint8_t **out, size_t *out_len)
{
	uint16_t phentsize, phnum;
	uint32_t phoff;
	size_t need, i, si;
	uint8_t *img;

	*out = NULL;
	*out_len = 0;
	if (!mdt || mdt_len < 52 || !segs)
		return -22;
	if (mdt[0] != 0x7f || mdt[1] != 'E' || mdt[2] != 'L' || mdt[3] != 'F')
		return -22;
	if (mdt[4] != 1 || mdt[5] != 1)
		return -22;

	phoff = ru32(mdt + 28);
	phentsize = ru16(mdt + 42);
	phnum = ru16(mdt + 44);
	if (phentsize < 32 || phnum == 0)
		return -22;
	if (phoff > mdt_len || (size_t)phnum * phentsize > mdt_len - phoff)
		return -22;

	need = mdt_len;
	si = 0;
	for (i = 0; i < phnum; i++) {
		const uint8_t *ph = mdt + phoff + i * phentsize;
		uint32_t off = ru32(ph + 4);
		uint32_t filesz = ru32(ph + 16);

		if (!filesz)
			continue;
		if (si >= nseg || segs[si].len != filesz)
			return -22;
		if ((size_t)off + filesz < off)
			return -22;
		if ((size_t)off + filesz > need)
			need = (size_t)off + filesz;
		si++;
	}
	if (si != nseg)
		return -22;

	img = calloc(1, need);
	if (!img)
		return -12;
	memcpy(img, mdt, mdt_len);

	si = 0;
	for (i = 0; i < phnum; i++) {
		const uint8_t *ph = mdt + phoff + i * phentsize;
		uint32_t off = ru32(ph + 4);
		uint32_t filesz = ru32(ph + 16);

		if (!filesz)
			continue;
		memcpy(img + off, segs[si].data, segs[si].len);
		si++;
	}

	*out = img;
	*out_len = need;
	return 0;
}
