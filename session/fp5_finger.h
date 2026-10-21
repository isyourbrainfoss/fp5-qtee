#ifndef FP5_FINGER_H
#define FP5_FINGER_H

/*
 * Finger list and remove for focal32. Pure, so the host tests cover the
 * payload layout and the remove gate without a phone.
 *
 * Opcodes are from the vendor HAL fingerprint.default.so (VT2Y.C.108):
 * ff_trustlet_enumerate loads {0x2005, 4} and ff_trustlet_remove loads
 * {0x2006, 8} from the {cmd, plen} table at 0x97a08 and 0x97a10. Marcus
 * Ramberg's focaltechqsee driver (libfprint e5912e8) uses the same ids.
 * 0x1028, sent after SET_GROUP since the first bring-up, is not in the
 * HAL table at all; focal32 answers it with its default -203.
 *
 * ENUMERATE (0x2005), plen 4:
 *   in   payload+0  u32 capacity (entries the caller has room for)
 *   out  status     the number of fingers (not an error code)
 *        payload+0  the same count
 *        payload+4  count entries of {u32 gid, u32 fid}, stride 8
 * The HAL copies count*8 bytes from payload+4 into the framework's
 * fingerprint_finger_id_t {gid, fid} array, and fails with -12 when the
 * count is above the capacity it passed. Marcus reads the fid at +8+8i,
 * which is the same word.
 *
 * REMOVE (0x2006), plen 8: payload+0 gid, payload+4 fid. Status 0 is
 * success. A finger id alone in the first word answers -200. The
 * removal is in RAM until SAVE (0x1014) writes the gallery back.
 */
#include <stddef.h>
#include <stdint.h>

#define FP5_OP_LIST 0x2005u
#define FP5_LIST_PLEN 4u
#define FP5_OP_REMOVE 0x2006u
#define FP5_REMOVE_PLEN 8u
/* Android allows 5 per user. The TA was sized for the HAL's caller. */
#define FP5_LIST_MAX 5
/* Bytes the reply needs past the payload start: count + MAX entries. */
#define FP5_LIST_REPLY (4u + 8u * FP5_LIST_MAX)

struct fp5_finger {
	uint32_t gid;
	uint32_t fid;
};

/* Writes the capacity word. pay must hold FP5_LIST_REPLY bytes; the
 * rest is cleared so a stale entry cannot be read back as a finger. */
void fp5_build_list(uint8_t *pay, size_t cap, uint32_t capacity);

/*
 * Decode an ENUMERATE answer. status is the request status word.
 * Returns the number of fingers written to out (0..max), or -1 when the
 * answer is not a list: negative status, a count above the capacity
 * sent, a payload count that disagrees with a positive status, or a
 * short buffer. fid 0 is never a finger and is rejected too.
 */
int fp5_parse_list(const uint8_t *pay, size_t n, int32_t status,
		   uint32_t capacity, struct fp5_finger *out, int max);

/* REMOVE payload. pay must hold 8 bytes. */
void fp5_build_remove(uint8_t *pay, size_t cap, uint32_t gid, uint32_t fid);

/* Parse a decimal finger id (what list prints). 0 on success. */
int fp5_parse_fid(const char *s, uint32_t *fid);

/*
 * The remove gate, checked before and after the trustlet is open.
 * argv-style: args are what follows "remove". Exactly one is a finger
 * id, and "--yes-really" must be present. Returns 0 and the fid, or
 * a negative reason:
 */
enum {
	FP5_RM_OK = 0,
	FP5_RM_NO_FID = -1,	/* no id, or not a positive decimal */
	FP5_RM_NO_YES = -2,	/* --yes-really missing */
	FP5_RM_EXTRA = -3,	/* an unknown flag or a second id */
	FP5_RM_NOT_LISTED = -4,	/* the id is not in the live list */
	FP5_RM_LAST = -5,	/* would remove the only finger */
};
int fp5_remove_args(int argc, char *const *argv, uint32_t *fid);
/* Second half: the id must be in the list the trustlet just gave. The
 * last finger is refused unless allow_last (--allow-last) is set, so a
 * typo cannot leave the phone with no finger at all. Writes the gid. */
int fp5_remove_check(const struct fp5_finger *list, int n, uint32_t fid,
		     int allow_last, uint32_t *gid);
const char *fp5_remove_reason(int rc);

/* 1 when fid is no longer in list (post-remove check). */
int fp5_list_lacks(const struct fp5_finger *list, int n, uint32_t fid);

#endif
