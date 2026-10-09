#include "fp5_wire.h"

#include <fcntl.h>
#include <string.h>

static uint32_t rd32(const uint8_t *p)
{
	uint32_t v;

	memcpy(&v, p, 4);
	return v;
}

static void wr32(uint8_t *p, uint32_t v)
{
	memcpy(p, &v, 4);
}

static uint16_t rd_be16(const uint8_t *p)
{
	return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

static void wr_be16(uint8_t *p, uint16_t v)
{
	p[0] = (uint8_t)(v >> 8);
	p[1] = (uint8_t)v;
}

uint32_t fp5_op_report(void) { return 0x1017; }
uint32_t fp5_op_wmode(void) { return 0x101f; }
uint32_t fp5_op_pre_enroll(void) { return 0x2000; }
uint32_t fp5_op_enroll(void) { return 0x2001; }
uint32_t fp5_op_save(void) { return 0x1014; }
uint32_t fp5_save_flag(void) { return 0x40000000u; }
uint32_t fp5_wmode_detect(void) { return 9; }
uint32_t fp5_wmode_touch(void) { return 1; }
uint32_t fp5_op_auth(void) { return 0x2008; }

uint32_t fp5_op_init(void) { return 0x1004; }
uint32_t fp5_op_init_spi(void) { return 0x1006; }
uint32_t fp5_op_init_dev(void) { return 0x100b; }
uint32_t fp5_op_sync(void) { return 0x100d; }
uint32_t fp5_op_probe(void) { return 0x100a; }
uint32_t fp5_op_query(void) { return 0x101c; }
uint32_t fp5_op_capture(void) { return 0x1013; }
uint32_t fp5_op_set_group(void) { return 0x2007; }
uint32_t fp5_op_enum(void) { return 0x1028; }
uint32_t fp5_op_set_spi(void) { return 0x1008; }
uint32_t fp5_op_version(void) { return 0x0205; }
uint32_t fp5_op_set_km(void) { return 0x1011; }
uint32_t fp5_op_chip(void) { return 0x1022; }
uint32_t fp5_op_calib(void) { return 0x100f; }
uint32_t fp5_op_health(void) { return 0x1016; }
uint32_t fp5_op_stats(void) { return 0x100e; }
uint32_t fp5_hal_flag_enroll(void) { return 0xC0040002u; }
uint32_t fp5_wmode_up(void) { return 2; }

void fp5_req_set(uint8_t *req, uint32_t cmd, uint32_t plen)
{
	wr32(req + 0, cmd);
	wr32(req + 4, plen);
	wr32(req + 8, 0);
}

void fp5_req_pay(uint8_t *req, const void *pay, uint32_t plen)
{
	if (pay && plen)
		memcpy(req + FP5_PAY_OFF, pay, plen);
}

uint32_t fp5_req_cmd(const uint8_t *req)
{
	return rd32(req);
}

int32_t fp5_req_rc(const uint8_t *req)
{
	int32_t v;

	memcpy(&v, req + 8, 4);
	return v;
}

uint32_t fp5_req_rem(const uint8_t *req)
{
	return rd32(req + FP5_PAY_OFF + FP5_REM_PAY);
}

void fp5_event_fill(uint8_t *pay, uint32_t ev, uint32_t flag, uint32_t burst,
		    uint32_t rounds)
{
	if (!burst)
		burst = 1;
	wr32(pay + 4, ev);
	wr32(pay + 0x2c8, burst);
	wr32(pay + 0x2d0, rounds);
	wr32(pay + 0x2d8, flag);
}

void fp5_capture_fill(uint8_t *pay, uint32_t burst, uint32_t frame, uint32_t flag)
{
	memset(pay, 0, 0x24);
	wr32(pay + 0x0c, burst);
	wr32(pay + 0x10, 1);
	wr32(pay + 0x14, frame);
	wr32(pay + 0x1c, flag);
	wr32(pay + 0x20, 0);
}

void fp5_poison_rem(uint8_t *pay)
{
	wr32(pay + 0x10, 0xaaaaaaaau);
	wr32(pay + 0x24, 0xa5a5a5a5u);
}

static void build_hdr(uint8_t *req, size_t cap, uint32_t cmd, uint32_t plen)
{
	if (cap < FP5_REQ_MIN)
		return;
	memset(req, 0, cap < 64 ? cap : 64);
	fp5_req_set(req, cmd, plen);
}

void fp5_build_report(uint8_t *req, size_t cap, uint32_t ev)
{
	build_hdr(req, cap, fp5_op_report(), 0x2e0);
	if (cap >= FP5_PAY_OFF + 0x2dc)
		fp5_event_fill(req + FP5_PAY_OFF, ev, 4, 3, 0);
}

void fp5_build_wmode(uint8_t *req, size_t cap, uint32_t mode)
{
	build_hdr(req, cap, fp5_op_wmode(), 4);
	if (cap >= FP5_PAY_OFF + 4)
		wr32(req + FP5_PAY_OFF, mode);
}

void fp5_build_enroll(uint8_t *req, size_t cap)
{
	uint8_t hat[0x4a];

	fp5_hat_enroll(hat, 0);
	build_hdr(req, cap, fp5_op_enroll(), 0x4a);
	if (cap >= FP5_PAY_OFF + 0x4a)
		memcpy(req + FP5_PAY_OFF, hat, 0x4a);
}

void fp5_build_save(uint8_t *req, size_t cap)
{
	uint32_t flag = fp5_save_flag();

	build_hdr(req, cap, fp5_op_save(), 4);
	if (cap >= FP5_PAY_OFF + 4)
		wr32(req + FP5_PAY_OFF, flag);
}

int fp5_enroll_is_gap(const uint8_t *req)
{
	/* focal32's switch covers 0x1004..0x1023 and 0x2000..0x2008.
	 * 0x1024 misses both and the TA returns the default -203.
	 */
	return fp5_req_cmd(req) == 0x1024u;
}

int fp5_report_is_fp6(const uint8_t *req)
{
	return fp5_req_cmd(req) == 0x1018u;
}

int fp5_save_ors_bit30_on_1015(const uint8_t *req)
{
	uint32_t cmd = fp5_req_cmd(req);
	uint32_t flag = rd32(req + FP5_PAY_OFF);

	return cmd == 0x1015u && (flag & 0x40000000u) != 0;
}

int fp5_real_down(uint32_t itype, int esd)
{
	return itype == 0x2u && !esd;
}

int fp5_score_sample(uint32_t avgv)
{
	return avgv > 0 && avgv < 600;
}

/*
 * Enrollment: every frame of the burst must be in range, so a template is
 * only ever built from full, good frames.
 */
int fp5_burst_full(const uint32_t *avgv, int n)
{
	int i, ok = 0;

	if (n < 3)
		return 0;
	for (i = 0; i < 3; i++) {
		if (fp5_score_sample(avgv[i]))
			ok++;
	}
	return ok == 3;
}

/*
 * Authentication: score the burst when at least one frame is in range.
 * Android always captures three frames and sends every one of them, including
 * a frame with avgv >= 600, when another frame is in range. A burst with no
 * frame in range is still not sent. The trustlet does the match.
 */
int fp5_burst_ok(const uint32_t *avgv, int n)
{
	return fp5_burst_pick(avgv, n) != 0;
}

/* The last in-range avgv of the burst, or 0 when none is in range. */
uint32_t fp5_burst_pick(const uint32_t *avgv, int n)
{
	int i;

	if (n > 3)
		n = 3;
	for (i = n - 1; i >= 0; i--) {
		if (fp5_score_sample(avgv[i]))
			return avgv[i];
	}
	return 0;
}

uint32_t fp5_auth_plen(void) { return 0xe; }

int fp5_auth_match(uint32_t itype, int esd, uint32_t avgv, int32_t report_rc,
		   uint32_t fid)
{
	if (!fp5_real_down(itype, esd))
		return 0;
	if (!fp5_score_sample(avgv))
		return 0;
	if (report_rc != 0)
		return 0;
	if (fid == 0 || fid == 0xaaaaaaaau || fid == 0xa5a5a5a5u)
		return 0;
	return 1;
}

int fp5_auth_burst(uint32_t itype, int esd, const uint32_t *avgv, int n,
		   int32_t report_rc, uint32_t fid)
{
	if (!fp5_real_down(itype, esd))
		return 0;
	if (!fp5_burst_ok(avgv, n))
		return 0;
	if (report_rc != 0)
		return 0;
	if (fid == 0 || fid == 0xaaaaaaaau || fid == 0xa5a5a5a5u)
		return 0;
	return 1;
}

/* Parsers stop at end so a token at the end of the buffer is not over-read. */
static uint32_t parse_hex(const char *p, const char *end)
{
	uint32_t v = 0;

	if (!p)
		return 0;
	if (end - p >= 2 && p[0] == '0' && (p[1] == 'x' || p[1] == 'X'))
		p += 2;
	while (p < end && *p) {
		uint32_t d;

		if (*p >= '0' && *p <= '9')
			d = (uint32_t)(*p - '0');
		else if (*p >= 'a' && *p <= 'f')
			d = (uint32_t)(*p - 'a' + 10);
		else if (*p >= 'A' && *p <= 'F')
			d = (uint32_t)(*p - 'A' + 10);
		else
			break;
		v = (v << 4) | d;
		p++;
	}
	return v;
}

static uint32_t parse_u(const char *p, const char *end)
{
	uint32_t v = 0;

	while (p < end && (*p == ' ' || *p == '\t'))
		p++;
	while (p < end && *p >= '0' && *p <= '9') {
		v = v * 10u + (uint32_t)(*p - '0');
		p++;
	}
	return v;
}

#define LOG_ITYPE "interrupt type"
#define LOG_QES "query event state: ["
#define LOG_ESD "esd]"
#define LOG_AVG_SP "avgv ="
#define LOG_AVG "avgv="
#define LIT_LEN(s) (sizeof(s) - 1)

/* 1 when the literal fits in the bytes left at p and matches there. */
static int at_lit(const char *p, const char *end, const char *lit, size_t len)
{
	return (size_t)(end - p) >= len && !memcmp(p, lit, len);
}

void fp5_scan_log(const uint8_t *buf, size_t n, struct fp5_log *out)
{
	size_t i;
	const char *base = (const char *)buf;
	const char *end = base + n;
	const char *last_itype = NULL;
	const char *last_qes = NULL;
	const char *last_avg = NULL;

	memset(out, 0, sizeof(*out));
	if (!buf)
		return;
	for (i = 0; i < n; i++) {
		const char *s = base + i;

		if (at_lit(s, end, LOG_ITYPE, LIT_LEN(LOG_ITYPE)))
			last_itype = s;
		else if (at_lit(s, end, LOG_QES, LIT_LEN(LOG_QES)))
			last_qes = s;
		else if (at_lit(s, end, LOG_AVG_SP, LIT_LEN(LOG_AVG_SP)) ||
			 at_lit(s, end, LOG_AVG, LIT_LEN(LOG_AVG)))
			last_avg = s;
	}
	if (last_itype) {
		const char *hx = NULL;
		size_t k, left = (size_t)(end - last_itype);

		for (k = 0; k + 2 < 48 && k + 1 < left && last_itype[k] &&
			    last_itype[k] != '\n'; k++) {
			if (last_itype[k] == '0' &&
			    (last_itype[k + 1] == 'x' || last_itype[k + 1] == 'X')) {
				hx = last_itype + k;
				break;
			}
		}
		out->itype = parse_hex(hx, end);
		out->saw_itype = hx != NULL;
		if (out->itype & 0x400u)
			out->esd = 1;
	}
	if (last_qes && at_lit(last_qes + LIT_LEN(LOG_QES), end, LOG_ESD,
			       LIT_LEN(LOG_ESD)))
		out->esd = 1;
	if (last_avg) {
		const char *p = last_avg;

		if (at_lit(p, end, LOG_AVG_SP, LIT_LEN(LOG_AVG_SP)))
			p += LIT_LEN(LOG_AVG_SP);
		else
			p += LIT_LEN(LOG_AVG);
		out->avgv = parse_u(p, end);
		out->saw_avgv = 1;
	}
}

int fp5_build_group(uint8_t *dst, size_t cap, const char *path)
{
	size_t n;

	if (!dst || !path || cap < 5)
		return -1;
	n = strlen(path);
	if (n > cap - 5)
		return -1;
	memset(dst, 0, cap);
	memcpy(dst + 4, path, n + 1);
	return (int)(4 + n + 1);
}

#include <stdio.h>

int fp5_sync_config(char *dst, size_t cap, int native_log)
{
	int n;

	/* 37777 (0x9391) is the focal32 profile that sets the feature
	   preprocessor. Detected id 37841 is not in that table. */
	n = snprintf(dst, cap,
		     "{\"driver\":{\"enable_spidev\":false,\"spi_bus_num\":14,"
		     "\"spi_c_s_num\":0,\"spi_on_demand\":true},"
		     "\"device\":{\"preferred_device_id\":37777,"
		     "\"spi_default_bps\":4000000,\"spi_mode\":0,"
		     "\"spi_capture_bps\":8030000,"
		     "\"vio_is_1p8\":true,\"enable_re_power_b4_probing\":true,"
		     "\"enable_hw_reset_b4_scanning\":false,"
		     "\"enable_prev_hw_process\":true,"
		     "\"enable_post_hw_process\":true,"
		     "\"image_acquisition_mode\":\"polling-driven\","
		     "\"burst_acquisition_num\":3,"
		     "\"burst_acquisition_mode\":1},"
		     "\"factory\":{\"other_press_inte\":128,\"dead_pixel_inte\":128},"
		     "\"trustlet\":{\"enable_trusted_enrollment\":false,"
		     "\"enable_authenticate_token\":false},"
		     "\"common\":{\"max_enrolling_fingers\":5,"
		     "\"max_enrolling_samples\":20,"
		     "\"enrolling_break_after_n_samples\":0,"
		     "\"image_processing_cols\":0,"
		     "\"image_processing_rows\":0,"
		     "\"persist_data_home\":\"/vendor/focaltech\"},"
		     "\"algorithm\":{\"min_enrolling_quality_threshold\":40,"
		     "\"min_enrolling_coverage_threshold\":70,"
		     "\"enrolling_overlap_intervals\":\"0x54A0\","
		     "\"enable_duplicated_finger_checking\":true,"
		     "\"min_identify_quality_threshold\":40,"
		     "\"min_identify_coverage_threshold\":70},"
		     "\"diagnosis\":{\"framework_log_level\":1,"
		     "\"firmware_log_level\":3,"
		     "\"algorithm_log_level\":2,"
		     "\"enable_algorithm_log\":true,"
		     "\"enable_logcat_trustlet\":true,"
		     "\"enable_trustlet_native_log\":%s,"
		     "\"enable_logcat_spi_data\":false}}",
		     native_log ? "true" : "false");
	if (n < 0 || (size_t)n >= cap)
		return -1;
	return n;
}

int fp5_log_is_score(const char *line)
{
	if (!line)
		return 0;
	return strstr(line, "FtVerifySubTemplate") != NULL &&
	       strstr(line, "score") != NULL;
}

int fp5_log_keep(const char *line)
{
	static const char *want[] = {
		"enroll", "Null", "error", "image", "remain", "statistic",
		"synced", "empty", "pointer", "core:", "trustlet", "authenticat",
	};
	int hex = 0, k, i;

	if (!line)
		return 0;
	if (fp5_log_is_score(line))
		return 1;
	if (strstr(line, "sn num") || strstr(line, "fullduplex") ||
	    strstr(line, "ISPI"))
		return 0;
	for (k = 0; line[k]; k++) {
		char c = line[k];

		if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
		    (c >= 'A' && c <= 'F'))
			hex++;
		else
			hex = 0;
		if (hex >= 20)
			return 0;
	}
	for (i = 0; i < (int)(sizeof(want) / sizeof(want[0])); i++) {
		if (strstr(line, want[i]))
			return 1;
	}
	return 0;
}

static const char *const gp_roots[] = {
	"tzstorage", "misc", "persist-data", "root3"
};

const char *fp5_gp_root(uint32_t root)
{
	if (root >= 4)
		return NULL;
	return gp_roots[root];
}

int fp5_gp_unlink_performs(void)
{
	return 0;
}

int fp5_gp_write_oflags(void)
{
	return O_RDWR | O_CREAT | O_SYNC;
}

int fp5_gp_is_template(const char *rel)
{
	return rel && strstr(rel, "ff_template_") != NULL;
}

int fp5_gp_header_is_template(const uint8_t *buf, size_t n)
{
	static const char key[] = "ff_template_";
	size_t klen = sizeof(key) - 1;
	size_t i;

	if (!buf || n < klen)
		return 0;
	if (n > 256)
		n = 256;
	for (i = 0; i + klen <= n; i++) {
		if (memcmp(buf + i, key, klen) == 0)
			return 1;
	}
	return 0;
}

int fp5_gp_decode(const uint8_t *sb, size_t len, struct fp5_gp *out)
{
	uint32_t op;

	memset(out, 0, sizeof(*out));
	if (!sb || len < 0x114)
		return -22;
	op = rd32(sb);
	out->op = op;
	if (op == 12) {
		out->ok = 1;
		out->init12 = 1;
		return 0;
	}
	if (op > 12)
		return -22;
	out->root = op >> 2;
	out->act = op & 3;
	if (out->root >= 4 || out->act > FP5_GP_UNLINK)
		return -22;
	memcpy(out->rel, sb + 4, 255);
	out->rel[255] = 0;
	if (!out->rel[0] || out->rel[0] == '/' || strstr(out->rel, ".."))
		return -22;
	out->off = (int32_t)rd32(sb + 0x104);
	out->len = rd32(sb + 0x108);
	if (out->len > 0x7d000)
		out->len = 0x7d000;
	out->ok = 1;
	return 0;
}

void fp5_gp_reply(uint8_t *sb, uint32_t err, uint32_t got)
{
	wr32(sb + 4, err);
	wr32(sb + 8, got);
}

void fp5_gp_init_reply(uint8_t *sb)
{
	uint64_t v = 2;

	memcpy(sb + 4, &v, 8);
}

size_t fp5_gp_init_resp_len(void)
{
	return 0xc;
}

#define RPMB_FRAME 512
#define RPMB_RR_OFF 510

static uint16_t frame_rr(const uint8_t *frm)
{
	return rd_be16(frm + RPMB_RR_OFF);
}

uint16_t fp5_rpmb_frame_result(const uint8_t *frame)
{
	return rd_be16(frame + 508);
}

uint32_t fp5_rpmb_frame_wc(const uint8_t *frame)
{
	uint32_t v = ((uint32_t)frame[504] << 24) | ((uint32_t)frame[505] << 16) |
		     ((uint32_t)frame[506] << 8) | frame[507];
	return v;
}

void fp5_rpmb_result_read_frame(uint8_t *frame)
{
	memset(frame, 0, RPMB_FRAME);
	wr_be16(frame + RPMB_RR_OFF, 0x0005);
}

void fp5_rpmb_get_wc_frame(uint8_t *frame)
{
	memset(frame, 0, RPMB_FRAME);
	wr_be16(frame + RPMB_RR_OFF, 0x0002);
}

void fp5_rpmb_arm_frame(uint8_t *frame, uint32_t cmd)
{
	if (cmd == 0x102 && frame_rr(frame) == 0) {
		memset(frame, 0, RPMB_FRAME);
		wr_be16(frame + RPMB_RR_OFF, 0x0002);
	}
}

void fp5_rpmb_reply_err(uint8_t *sb, uint32_t err)
{
	wr32(sb + 4, err);
}

void fp5_rpmb_place_multi_write(uint8_t *sb, const uint8_t *frame)
{
	memcpy(sb + 0x14, frame, RPMB_FRAME);
	wr32(sb + 4, 0);
	wr32(sb + 8, RPMB_FRAME);
	wr32(sb + 0xc, 0x14);
}

void fp5_rpmb_place_multi_read(uint8_t *sb, uint32_t off, const uint8_t *data,
			       uint32_t nbytes)
{
	memcpy(sb + off, data, nbytes);
	wr32(sb + 4, 0);
	wr32(sb + 8, nbytes);
}

void fp5_rpmb_place_single_ok(uint8_t *sb)
{
	wr32(sb + 4, 0);
}

void fp5_rpmb_fill_info(uint8_t *sb, uint32_t write_counter)
{
	wr32(sb + 4, 0);
	wr32(sb + 0x10, 0x40);
	wr32(sb + 0x14, 512);
	wr32(sb + 0x18, 0x40u * 512u);
	wr32(sb + 0x1c, write_counter);
}

int fp5_rpmb_plan(const uint8_t *sb, size_t len, struct fp5_rpmb *out)
{
	static const uint32_t prefer[] = { 0, 0x18, 0x10, 0x14, 0x20, 0x8 };
	uint32_t cmd, nfr, hdr_off, hdr_sz;
	uint32_t offs[8];
	unsigned n_off = 0, i;
	int saw_rr1 = 0;

	memset(out, 0, sizeof(*out));
	if (!sb || len < RPMB_FRAME + 0x20) {
		out->kind = FP5_RPMB_FAIL;
		return -22;
	}
	cmd = rd32(sb);
	out->cmd = cmd;
	if (cmd == 0x101) {
		out->kind = FP5_RPMB_REFUSE;
		return 0;
	}
	if (cmd == 0x104) {
		out->kind = FP5_RPMB_GET_INFO;
		return 0;
	}
	if (cmd != 0x102 && cmd != 0x103) {
		out->kind = FP5_RPMB_FAIL;
		return 0;
	}
	hdr_off = rd32(sb + 0xc);
	hdr_sz = rd32(sb + 8);
	nfr = rd32(sb + 4);
	if (nfr < 1 || nfr > 16)
		nfr = 1;
	out->nfr = nfr;
	if (hdr_sz >= RPMB_FRAME && hdr_off + RPMB_FRAME <= len)
		offs[n_off++] = hdr_off;
	for (i = 1; i < 6; i++)
		offs[n_off++] = prefer[i];

	for (i = 0; i < n_off; i++) {
		uint32_t off = offs[i];
		uint16_t rr;
		const uint8_t *frm;

		if (off + RPMB_FRAME > len)
			continue;
		frm = sb + off;
		rr = frame_rr(frm);
		if (cmd == 0x102 && rr == 0)
			rr = 0x0002;
		if (rr < 1 || rr > 5)
			continue;
		if (rr == 1) {
			saw_rr1 = 1;
			continue;
		}
		out->off = off;
		out->rr = rr;
		out->single_rr3_persist = (rr == 3);
		if (cmd == 0x103 && rr == 3 && nfr > 1 &&
		    off + (size_t)nfr * RPMB_FRAME <= len) {
			uint32_t w4 = rd32(sb + 0x14);
			uint32_t out_n = (w4 >= 1 && w4 <= 16) ? w4 : nfr;
			size_t out_len = (size_t)out_n * RPMB_FRAME;

			if (out_len <= 4096 && off + out_len <= len) {
				out->kind = FP5_RPMB_MULTI_WRITE;
				out->out_n = out_n;
				out->persist_on_result0 = 1;
				out->single_rr3_persist = 0;
				return 0;
			}
		}
		if (cmd == 0x102 && nfr > 1 && off + (size_t)nfr * RPMB_FRAME <= len) {
			out->kind = FP5_RPMB_MULTI_READ;
			out->out_n = nfr;
			return 0;
		}
		out->kind = FP5_RPMB_SINGLE;
		out->out_n = 1;
		return 0;
	}
	out->kind = saw_rr1 ? FP5_RPMB_REFUSE : FP5_RPMB_FAIL;
	return 0;
}

void fp5_time_apply(uint8_t *sb, size_t len, int64_t sec, int32_t nsec)
{
	uint32_t cmd;
	int64_t days, z, era, doe, yoe, y, doy, mp, d, m;
	int wday;
	uint32_t year, mon, mday, hour, min, ssec;

	/* Replies write at most 40 bytes. Clear the 64-byte reply area, but
	 * never past the listener buffer the caller handed us. */
	if (!sb || len < 48)
		return;
	cmd = rd32(sb);
	memset(sb, 0, len < 64 ? len : 64);
	if (sec < 0)
		sec = 0;
	switch (cmd) {
	case 0x302:
		wr32(sb + 4, (uint32_t)sec);
		break;
	case 0x303:
		days = sec / 86400;
		ssec = (uint32_t)(sec % 86400);
		hour = ssec / 3600;
		min = (ssec % 3600) / 60;
		ssec = ssec % 60;
		wday = (int)((days + 4) % 7);
		z = days + 719468;
		era = (z >= 0 ? z : z - 146096) / 146097;
		doe = z - era * 146097;
		yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
		y = yoe + era * 400;
		doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
		mp = (5 * doy + 2) / 153;
		d = doy - (153 * mp + 2) / 5 + 1;
		m = mp + (mp < 10 ? 3 : -9);
		y += (m <= 2);
		year = (uint32_t)(y - 1900);
		mon = (uint32_t)(m - 1);
		mday = (uint32_t)d;
		wr32(sb + 4, ssec);
		wr32(sb + 8, min);
		wr32(sb + 12, hour);
		wr32(sb + 16, mday);
		wr32(sb + 20, mon);
		wr32(sb + 24, year);
		wr32(sb + 28, (uint32_t)wday);
		wr32(sb + 32, (uint32_t)(doy > 305 ? doy - 306 : doy + 59));
		wr32(sb + 36, 0);
		break;
	case 0x304: {
		uint64_t ms = (uint64_t)(uint32_t)sec * 1000ull;

		memcpy(sb + 4, &ms, 8);
		break;
	}
	case 0x305:
		wr32(sb + 4, (uint32_t)sec);
		wr32(sb + 8, (uint32_t)nsec);
		break;
	case 0x306:
		break;
	default:
		wr32(sb + 4, (uint32_t)-1);
		break;
	}
}

void fp5_ssd_apply(uint8_t *sb, size_t len)
{
	if (!sb || len < 8)
		return;
	wr32(sb + 4, 0);
}

void fp5_hat_auth(uint8_t *hat)
{
	memset(hat, 0, 0x4a);
	hat[0x1c] = 2;
	hat[0x49] = 1;
}

void fp5_hat_enroll(uint8_t *hat, uint64_t challenge)
{
	memset(hat, 0, 0x4a);
	memcpy(hat + 1, &challenge, 8);
	hat[0x1c] = 2;
	hat[0x49] = 1;
}
