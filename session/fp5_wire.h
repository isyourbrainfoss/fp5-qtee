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
uint32_t fp5_save_flag(void);     /* 0x40000000 */
uint32_t fp5_wmode_detect(void);  /* 9, SPI helper used while enroll is scanning */
uint32_t fp5_wmode_touch(void);   /* 1, chip wait-touch; TA names it FDT_DOWN_DETECT */
uint32_t fp5_op_auth(void);       /* 0x2008 */

uint32_t fp5_op_init(void);
uint32_t fp5_op_init_spi(void);
uint32_t fp5_op_init_dev(void);
uint32_t fp5_op_sync(void);
uint32_t fp5_op_probe(void);
uint32_t fp5_op_query(void);
uint32_t fp5_op_capture(void);
uint32_t fp5_op_set_group(void);
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
/* 1 to capture another burst. Stops at 3, and when the finger has lifted. */
int fp5_retry_more(int bursts_done, int held);
/* One press is one strike, however many scored bursts missed. */
int fp5_press_strikes(int scored_misses);

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
/* 1 when the session should print this qsee line. Score lines always pass. */
int fp5_log_keep(const char *line);

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

#endif
