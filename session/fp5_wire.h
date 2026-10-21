#ifndef FP5_WIRE_H
#define FP5_WIRE_H

/*
 * FP5 focal32 command bytes, listener replies, and enroll/auth decisions.
 * No TEE, GPIO, or file calls. The session shell is the only caller of those.
 */
#include <stddef.h>
#include <stdint.h>

#define FP5_PAY_OFF 0x10
#define FP5_REM_PAY 0x24
#define FP5_REQ_MIN 0x14

/* Contract opcodes. The session calls these functions. */
uint32_t fp5_op_report(void);     /* 0x1017 */
uint32_t fp5_op_wmode(void);      /* 0x101f */
uint32_t fp5_op_pre_enroll(void); /* 0x2000, ff_trustlet_pre_enroll */
uint32_t fp5_op_enroll(void);     /* 0x2001, ff_trustlet_enroll */
uint32_t fp5_op_save(void);       /* 0x1014 */
uint32_t fp5_save_flag(void);     /* 0x40000000, Android HAL save. Not 0x60000000. */
uint32_t fp5_wmode_detect(void);  /* 9, SPI helper used while enroll is scanning */
uint32_t fp5_wmode_touch(void);   /* 1, chip wait-touch; TA names it FDT_DOWN_DETECT */
uint32_t fp5_op_auth(void);       /* 0x2008 */

uint32_t fp5_op_init(void);
uint32_t fp5_op_init_spi(void);
/* 0x1007 FF_CMD_TA_FREE_SPI and 0x2004 CANCEL. Both from Marcus
 * Ramberg's libfprint focaltechqsee driver (focaltech-qsee-proto.h). */
uint32_t fp5_op_free_spi(void);
uint32_t fp5_op_cancel(void);
uint32_t fp5_op_init_dev(void);
uint32_t fp5_op_sync(void);
uint32_t fp5_op_probe(void);
uint32_t fp5_op_query(void);
uint32_t fp5_op_capture(void);
uint32_t fp5_op_set_group(void);
/* 0x1028. Not a focal32 command: the HAL table has no 0x1028 and the
 * TA answers its default -203. Still sent after SET_GROUP on the unlock
 * path, unchanged. The real ENUMERATE is 0x2005 (fp5_finger.h), used by
 * the list mode. See docs/finger-manage.md. */
uint32_t fp5_op_enum(void);
uint32_t fp5_op_set_spi(void);
uint32_t fp5_op_version(void);
uint32_t fp5_op_set_km(void);
uint32_t fp5_op_chip(void);
uint32_t fp5_op_calib(void);
uint32_t fp5_op_health(void);
uint32_t fp5_op_stats(void);      /* 0x100e, plen 0x230 */
uint32_t fp5_hal_flag_enroll(void);
uint32_t fp5_wmode_up(void);

void fp5_req_set(uint8_t *req, uint32_t cmd, uint32_t plen);
void fp5_req_pay(uint8_t *req, const void *pay, uint32_t plen);
uint32_t fp5_req_cmd(const uint8_t *req);
int32_t fp5_req_rc(const uint8_t *req);
uint32_t fp5_req_rem(const uint8_t *req);

void fp5_build_report(uint8_t *req, size_t cap, uint32_t ev);
void fp5_build_wmode(uint8_t *req, size_t cap, uint32_t mode);
void fp5_build_enroll(uint8_t *req, size_t cap);
void fp5_build_save(uint8_t *req, size_t cap);

/* 1 when the buffer is a forbidden command. */
int fp5_enroll_is_gap(const uint8_t *req);      /* 0x1024, TA default -203 */
int fp5_report_is_fp6(const uint8_t *req);      /* 0x1018 used as report */
int fp5_save_ors_bit30_on_1015(const uint8_t *req);

void fp5_event_fill(uint8_t *pay, uint32_t ev, uint32_t flag, uint32_t burst,
		    uint32_t rounds);
void fp5_capture_fill(uint8_t *pay, uint32_t burst, uint32_t frame, uint32_t flag);
void fp5_poison_rem(uint8_t *pay);

/* Exact interrupt type 0x2 and not ESD. 0x212 is not a down. */
int fp5_real_down(uint32_t itype, int esd);
/* A query at arm, before any new edge. No printed type is not a finger.
 * Leftovers and ESD are not a finger. */
int fp5_down_at_arm(int saw_itype, uint32_t itype, int esd);
/* The panel came on while this attempt was already waiting. Query even
 * though irq_count did not move. */
int fp5_query_on_panel_rise(int was_on, int now_on);
/* Average value in (0, 600). */
int fp5_score_sample(uint32_t avgv);
/* Enrollment: all 3 frames in range. */
int fp5_burst_full(const uint32_t *avgv, int n);
/* Authentication: at least one of the first 3 frames in range.
 * A sibling at avgv >= 600 does not drop the burst. All 3 frames are sent.
 */
int fp5_burst_ok(const uint32_t *avgv, int n);
/* Last in-range avgv of the first 3 frames, 0 if none. */
uint32_t fp5_burst_pick(const uint32_t *avgv, int n);
uint32_t fp5_auth_plen(void); /* 0xe, HAL ff_trustlet_authenticate */
/* Single-frame gate. avgv must itself be in range. */
int fp5_auth_match(uint32_t itype, int esd, uint32_t avgv, int32_t report_rc,
		   uint32_t fid);
/* Burst gate. One in-range frame is enough; a hot sibling is still sent.
 * Report rc 0 and a real template id. Poison and fid 0 are not a match.
 */
int fp5_auth_burst(uint32_t itype, int esd, const uint32_t *avgv, int n,
		   int32_t report_rc, uint32_t fid);
/* 1 while this press is still down. An unchanged irq_count is still down.
 * An edge with no classified type, or any type other than exact 0x2, has
 * lifted. Do not treat that edge as a new finger-down.
 */
int fp5_finger_held(unsigned base, unsigned now, int saw_itype,
		    uint32_t itype, int esd);
/* Same as fp5_finger_held, except an already-down press whose next query
 * is idle 0x0. That query is the report's leftover, not a lift, so the
 * press keeps its remaining bursts. A lift (0x4), ESD, or any other type
 * still stops. already is 0 on a fresh edge.
 */
int fp5_held_for_retry(int already, unsigned base, unsigned now,
		       int saw_itype, uint32_t itype, int esd);
/* 1 to capture another burst. Stops at 3, and when the finger has lifted. */
int fp5_retry_more(int bursts_done, int held);
/*
 * Rescan policy of the Android HAL on this phone. Its focaltech config
 * has common.max_authentication_rescan_times (5 on the July 2026 build;
 * the C.108 build logs "rescan and try again (n/2)", so 1 + 2 scans) and
 * common.max_authentication_rescan_timing = 1000 ms. A rescan happens
 * only while the finger is still down. The HAL has no "falling" rule: on
 * 2026-10-09 it scored rescans whose avgv fell 221 -> 181 -> 170.
 * Build-time defaults; a root run may override them with
 * FP5_QTEE_AUTH_SCANS, FP5_QTEE_RESCAN_MS and FP5_QTEE_SKIP_FALLING
 * (sudo drops the environment, so the watcher always gets the defaults).
 */
#ifndef FP5_AUTH_MAX_SCANS
#define FP5_AUTH_MAX_SCANS 3
#endif
#ifndef FP5_AUTH_RESCAN_MS
#define FP5_AUTH_RESCAN_MS 1000
#endif
#ifndef FP5_SKIP_FALLING
#define FP5_SKIP_FALLING 0
#endif
#define FP5_AUTH_SCANS_MAX 8
/* Clamp a scan count to 1..FP5_AUTH_SCANS_MAX. */
int fp5_auth_scans_clamp(long scans);
/*
 * 1 to capture another burst: the finger is still down, fewer than
 * max_scans bursts were captured, and less than budget_ms have passed
 * since the finger-down. budget_ms <= 0 means no time limit. The first
 * burst is never a rescan (scans_done 0 returns 0).
 */
int fp5_rescan_more(int scans_done, int held, long elapsed_ms,
		    int max_scans, long budget_ms);
/*
 * One timing line: "TIME <which> <event> <ms>[ <extra>]". ms is
 * monotonic since the session started. The line never starts with
 * "AUTH", so the watcher's patterns do not see it. Returns the length,
 * or -1 when it does not fit or event is empty.
 */
int fp5_time_line(char *dst, size_t cap, int which, const char *event,
		  long ms, const char *extra);
/* Brightest in-range frame of the first 3. 0 when none is in (0, 600). */
uint32_t fp5_burst_level(const uint32_t *avgv, int n);
/* A later burst this far below the first burst's level is a lifting
 * finger. On 2026-10-11 a second burst 63 below (332 to 269) still
 * matched. A burst 83 below (241 to 158) did not. 80 keeps the match.
 */
#define FP5_FALL_AVGV 80
/* 1 when now is at least FP5_FALL_AVGV below first. 0 if either is 0. */
int fp5_burst_falling(uint32_t first, uint32_t now);
/* One press is one strike, however many scored bursts missed. */
int fp5_press_strikes(int scored_misses);
/* 1 when a scored miss had no nonzero subtemplate score, so the same
 * process should match the next press. A nonzero score ends it.
 */
int fp5_miss_stays(int scored, int nonzero);

struct fp5_log {
	uint32_t itype;
	int esd;
	uint32_t avgv;
	int saw_itype;
	int saw_avgv;
};

void fp5_scan_log(const uint8_t *buf, size_t n, struct fp5_log *out);

/* SET_GROUP payload: a zero word, then the NUL-terminated group path. */
#define FP5_GROUP_CAP 128
/* Fills dst (cap bytes). Returns the payload length, or -1 when path is
 * NULL or does not fit with its NUL. dst is unchanged on -1. */
int fp5_build_group(uint8_t *dst, size_t cap, const char *path);

/* Nested SYNC_CFG JSON. native_log is the trustlet native-log bool. */
int fp5_sync_config(char *dst, size_t cap, int native_log);

/* 1 when the line is the subtemplate distance diary. */
int fp5_log_is_score(const char *line);
/* 1 and writes *score when line is that diary. 0 leaves *score alone. */
int fp5_log_score(const char *line, int *score);
/* 1 when any score diary in buf is not zero. */
int fp5_log_nonzero_score(const uint8_t *buf, size_t n);
/* 1 when the session should print this qsee line. Score lines always pass. */
int fp5_log_keep(const char *line);
/* Last "there is/are N template(s) loaded" in buf. Returns 1 when the
 * phrase is present and writes N to *count. A missing phrase returns 0
 * and leaves *count alone.
 */
int fp5_gallery_loaded(const uint8_t *buf, size_t n, int *count);

/* GPFILE. unlink_performs is always 0. write flags never include O_TRUNC. */
#define FP5_GP_READ 0
#define FP5_GP_WRITE 1
#define FP5_GP_UNLINK 2

struct fp5_gp {
	int ok;
	int init12;
	uint32_t op;
	uint32_t root;
	uint32_t act;
	char rel[256];
	int32_t off;
	uint32_t len;
};

int fp5_gp_decode(const uint8_t *sb, size_t len, struct fp5_gp *out);
void fp5_gp_reply(uint8_t *sb, uint32_t err, uint32_t got);
void fp5_gp_init_reply(uint8_t *sb);
/* libdrmfs command 12: QSEECom_send_resp length, opcode plus the u64. */
size_t fp5_gp_init_resp_len(void);
const char *fp5_gp_root(uint32_t root);
int fp5_gp_unlink_performs(void);
int fp5_gp_write_oflags(void);
int fp5_gp_is_template(const char *rel);
/* Object header (first page), not the GPFILE relative name. */
int fp5_gp_header_is_template(const uint8_t *buf, size_t n);

/* RPMB. Cmd 0x101 and req_resp 1 are refused. No key program. */
#define FP5_RPMB_REFUSE 1
#define FP5_RPMB_GET_INFO 2
#define FP5_RPMB_MULTI_WRITE 3
#define FP5_RPMB_MULTI_READ 4
#define FP5_RPMB_SINGLE 5
#define FP5_RPMB_FAIL 6

struct fp5_rpmb {
	int kind;
	uint32_t cmd;
	uint32_t off;
	uint32_t nfr;
	uint32_t out_n;
	uint16_t rr;
	int persist_on_result0;
	int single_rr3_persist;
};

int fp5_rpmb_plan(const uint8_t *sb, size_t len, struct fp5_rpmb *out);
void fp5_rpmb_reply_err(uint8_t *sb, uint32_t err);
void fp5_rpmb_place_multi_write(uint8_t *sb, const uint8_t *frame);
void fp5_rpmb_place_multi_read(uint8_t *sb, uint32_t off, const uint8_t *data,
			       uint32_t nbytes);
void fp5_rpmb_place_single_ok(uint8_t *sb);
void fp5_rpmb_fill_info(uint8_t *sb, uint32_t write_counter);
uint16_t fp5_rpmb_frame_result(const uint8_t *frame);
uint32_t fp5_rpmb_frame_wc(const uint8_t *frame);
void fp5_rpmb_result_read_frame(uint8_t *frame);
void fp5_rpmb_get_wc_frame(uint8_t *frame);
void fp5_rpmb_arm_frame(uint8_t *frame, uint32_t cmd);

void fp5_time_apply(uint8_t *sb, size_t len, int64_t sec, int32_t nsec);
void fp5_ssd_apply(uint8_t *sb, size_t len);

void fp5_hat_auth(uint8_t *hat);
/* PRE_ENROLL challenge at hat+1, token version 0, authenticator type at +0x1c. */
void fp5_hat_enroll(uint8_t *hat, uint64_t challenge);

/*
 * Template learning, as the Android HAL does it after every
 * authentication result (device_event_thread in fingerprint.default.so,
 * from 0x527b4). The HAL table entry is {0x1015, 0x2e0}
 * (ff_trustlet_update_template). The payload is zero except:
 *   +0x2c8, +0x2d0  copied from the report event context (after the TA
 *                   answered the last 0x1017 report)
 *   +0x2d8          0x80000 (8 bytes 00 00 08 00 00 00 00 00 at 0x97998)
 * The TA answers byte +0x2a4 != 0 when the template changed. The HAL then
 * queues a save with mask 0x60000000 (lazy timer, 5000 ms) that ends in
 * ff_trustlet_save_data: 0x1014, length 4, payload = mask. 0x1015 alone
 * changes only the RAM copy.
 * A report answer with bit 0x40000 at +0x2d8 (byte 0x2d9 bit 2) and rc
 * 0 or -11 queues a save with mask 0x80000000 (0x51b98).
 */
#ifndef FP5_LEARN
#define FP5_LEARN 1
#endif
#define FP5_TPL_PLEN 0x2e0u
#define FP5_LEARN_SAVE_MASK 0x60000000u
#define FP5_REPORT_SAVE_MASK 0x80000000u
uint32_t fp5_op_update_template(void); /* 0x1015 */
void fp5_tpl_update_fill(uint8_t *pay, uint32_t w2c8, uint32_t w2d0);
int fp5_tpl_update_changed(const uint8_t *pay);
uint32_t fp5_tpl_update_stats(const uint8_t *pay);
int fp5_report_wants_save(const uint8_t *pay, int32_t rc);

#endif
