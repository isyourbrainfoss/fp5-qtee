#include "fp5_wire.h"

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails;

static void expect(int cond, const char *msg)
{
	if (!cond) {
		fprintf(stderr, "FAIL %s\n", msg);
		fails++;
	}
}

static void put_frame_rr(uint8_t *frame, uint16_t rr, uint16_t result)
{
	memset(frame, 0, 512);
	frame[508] = (uint8_t)(result >> 8);
	frame[509] = (uint8_t)result;
	frame[510] = (uint8_t)(rr >> 8);
	frame[511] = (uint8_t)rr;
}

static void test_opcodes(void)
{
	uint8_t req[0x400];
	uint8_t bad[64];

	expect(fp5_op_report() == 0x1017, "report opcode");
	expect(fp5_op_wmode() == 0x101f, "wmode opcode");
	expect(fp5_op_pre_enroll() == 0x2000, "pre-enroll opcode");
	expect(fp5_op_enroll() == 0x2001, "enroll opcode");
	expect(fp5_op_enroll() != 0x1024, "enroll is not the gap opcode");
	expect(fp5_op_save() == 0x1014, "save opcode");
	expect(fp5_save_flag() == 0x40000000u, "save flag");
	expect(fp5_wmode_detect() == 9, "detect mode");
	expect(fp5_wmode_touch() == 1, "touch mode");
	expect(fp5_op_report() != 0x1018, "report is not 0x1018");
	expect(fp5_op_stats() == 0x100e, "stats opcode");

	{
		uint8_t hat[0x4a];
		uint64_t ch = 0x1122334455667788ull;

		fp5_hat_enroll(hat, ch);
		expect(hat[0] == 0, "token version 0");
		expect(memcmp(hat + 1, &ch, 8) == 0, "challenge at hat+1");
		expect(hat[0x1c] == 2, "authenticator type");
		expect(hat[0x49] == 1, "enroll hat flag");
	}
	fp5_build_enroll(req, sizeof(req));
	expect(fp5_req_cmd(req) == fp5_op_enroll(), "built enroll");
	expect(*(uint32_t *)(req + 4) == 0x4a, "enroll plen 0x4a");
	expect(!fp5_enroll_is_gap(req), "built enroll is in the switch");
	memset(bad, 0, sizeof(bad));
	fp5_req_set(bad, 0x1024, 0);
	expect(fp5_enroll_is_gap(bad), "0x1024 is the default-reject opcode");

	fp5_build_report(req, sizeof(req), 5);
	expect(fp5_req_cmd(req) == fp5_op_report(), "built report");
	expect(!fp5_report_is_fp6(req), "built report not FP6");
	expect(fp5_req_cmd(req) != 0x1018, "report cmd byte");
	fp5_req_set(bad, 0x1018, 0x2e0);
	expect(fp5_report_is_fp6(bad), "0x1018 is not the report");

	fp5_build_wmode(req, sizeof(req), fp5_wmode_detect());
	expect(fp5_req_cmd(req) == fp5_op_wmode(), "built wmode");
	expect(*(uint32_t *)(req + 0x10) == 9, "wmode payload 9");
	fp5_build_wmode(req, sizeof(req), fp5_wmode_touch());
	expect(*(uint32_t *)(req + 0x10) == 1, "wmode payload 1");

	fp5_build_save(req, sizeof(req));
	expect(fp5_req_cmd(req) == 0x1014, "save cmd");
	expect(*(uint32_t *)(req + 0x10) == 0x40000000u, "save flag word");
	expect(!fp5_save_ors_bit30_on_1015(req), "save is not 0x1015|bit30");
	memset(bad, 0, sizeof(bad));
	fp5_req_set(bad, 0x1015, 4);
	*(uint32_t *)(bad + 0x10) = 0x40000000u;
	expect(fp5_save_ors_bit30_on_1015(bad), "0x1015|bit30 is forbidden");
}

static void test_decisions(void)
{
	uint32_t avg[3] = { 300, 280, 264 };
	uint8_t log[128];
	struct fp5_log sl;

	expect(fp5_real_down(0x2, 0), "exact 0x2");
	expect(!fp5_real_down(0x212, 0), "0x212 leftover");
	expect(!fp5_real_down(0x2, 1), "esd blocks down");
	expect(!fp5_real_down(0x12, 0), "0x12 is not down");
	expect(fp5_down_at_arm(1, 0x2, 0), "held finger at arm");
	expect(!fp5_down_at_arm(0, 0x2, 0), "query with no type is not a finger");
	expect(!fp5_down_at_arm(1, 0x212, 0), "0x212 at arm is leftover");
	expect(!fp5_down_at_arm(1, 0x2, 1), "esd at arm is not a finger");
	expect(!fp5_down_at_arm(1, 0x0, 0), "idle 0 at arm");
	expect(!fp5_down_at_arm(1, 0x1, 0), "idle 1 at arm");
	expect(!fp5_down_at_arm(1, 0x210, 0), "0x210 at arm is not a down");
	expect(fp5_query_on_panel_rise(0, 1), "panel rise queries");
	expect(!fp5_query_on_panel_rise(1, 1), "panel staying on does not");
	expect(!fp5_query_on_panel_rise(0, 0), "panel staying off does not");
	expect(!fp5_query_on_panel_rise(1, 0), "panel fall does not");
	expect(fp5_score_sample(300), "avgv 300");
	expect(!fp5_score_sample(966), "empty avgv");
	expect(!fp5_score_sample(0), "avgv 0");
	expect(!fp5_score_sample(600), "avgv 600");
	expect(fp5_burst_ok(avg, 3), "three good frames");
	expect(fp5_burst_full(avg, 3), "enroll: three good frames");
	avg[2] = 900;
	expect(!fp5_burst_full(avg, 3), "enroll: one empty frame rejects");
	expect(fp5_burst_ok(avg, 3), "auth: two good frames are scored");
	expect(fp5_burst_pick(avg, 3) == 280, "auth: picks last in-range frame");
	{
		uint32_t light[3] = { 966, 412, 0 };
		uint32_t none[3] = { 0, 966, 600 };
		uint32_t first[3] = { 250, 0, 0 };

		expect(fp5_burst_ok(light, 3), "light tap: one good frame");
		expect(fp5_burst_pick(light, 3) == 412, "light tap: uses 412");
		expect(fp5_burst_ok(first, 3), "only first frame good");
		expect(fp5_burst_pick(first, 3) == 250, "only first frame picked");
		expect(!fp5_burst_ok(none, 3), "no in-range frame rejects");
		expect(fp5_burst_pick(none, 3) == 0, "no in-range frame picks 0");
		expect(!fp5_burst_ok(light, 2) || fp5_burst_pick(light, 2) == 412,
		       "short burst only looks at n frames");
		expect(!fp5_burst_ok(first + 1, 2), "two empty frames reject");
		expect(!fp5_burst_full(light, 3), "light tap never enrolls");
		/* The pick is in range, but the trustlet still decides. */
		expect(!fp5_auth_match(0x2, 0, fp5_burst_pick(light, 3), -1,
				       1253023920u), "light tap: trustlet miss");
		expect(!fp5_auth_match(0x2, 0, fp5_burst_pick(light, 3), 0, 0),
		       "light tap: fid 0 is not a match");
		expect(!fp5_auth_match(0x2, 0, fp5_burst_pick(none, 3), 0,
				       1253023920u), "no frame: never a match");
		expect(fp5_auth_match(0x2, 0, fp5_burst_pick(light, 3), 0,
				      1253023920u), "light tap: trustlet hit");
		/* Android 22:23:38: one frame at 638, and the TA still scored it. */
		{
			uint32_t mixed[3] = { 328, 521, 638 };

			expect(fp5_burst_ok(mixed, 3), "mixed burst is scored");
			expect(!fp5_burst_full(mixed, 3), "mixed burst does not enroll");
			expect(fp5_burst_pick(mixed, 3) == 521, "mixed pick skips 638");
			expect(fp5_auth_burst(0x2, 0, mixed, 3, 0, 1253023920u),
			       "mixed burst: trustlet hit");
			expect(!fp5_auth_burst(0x2, 0, mixed, 3, -2, 1253023920u),
			       "mixed burst: trustlet miss");
			expect(!fp5_auth_burst(0x2, 0, mixed, 3, 0, 0),
			       "mixed burst: fid 0");
			expect(!fp5_auth_burst(0x212, 0, mixed, 3, 0, 1253023920u),
			       "mixed burst: leftover is not a hit");
			expect(!fp5_auth_match(0x2, 0, 638, 0, 1253023920u),
			       "a lone hot frame is not a match");
		}
	}
	expect(fp5_auth_plen() == 0xe, "auth plen");
	expect(fp5_auth_match(0x2, 0, 300, 0, 1253023920u), "match");
	expect(!fp5_auth_match(0x212, 0, 300, 0, 1253023920u), "leftover is not a match");
	expect(!fp5_auth_match(0x2, 0, 966, 0, 1253023920u), "empty is not a match");
	expect(!fp5_auth_match(0x2, 0, 300, -1, 1253023920u), "report err is not a match");
	expect(!fp5_auth_match(0x2, 0, 300, 0, 0), "fid 0 is not a match");
	expect(!fp5_auth_match(0x2, 0, 300, 0, 0xaaaaaaaau), "poison is not a match");
	expect(!fp5_auth_match(0x2, 0, 300, 0, 0xa5a5a5a5u),
	       "second poison is not a match");
	expect(!fp5_auth_match(0x2, 0, 300, -11, 1253023920u),
	       "report -11 is not a match");
	expect(fp5_finger_held(4, 4, 0, 0, 0), "unchanged irq is still down");
	expect(fp5_finger_held(4, 5, 1, 0x2, 0), "another exact down is held");
	expect(!fp5_finger_held(4, 5, 0, 0x2, 0), "unclassified edge stops");
	expect(!fp5_finger_held(4, 5, 1, 0x4, 0), "lift stops the retry");
	expect(!fp5_finger_held(4, 5, 1, 0x212, 0), "leftover edge stops");
	expect(!fp5_finger_held(4, 5, 1, 0x2, 1), "esd on the edge stops");
	expect(!fp5_retry_more(0, 1), "no burst yet is not a retry");
	expect(fp5_retry_more(1, 1), "retry after the first burst");
	expect(fp5_retry_more(2, 1), "retry after the second burst");
	expect(!fp5_retry_more(3, 1), "three bursts is the limit");
	expect(!fp5_retry_more(1, 0), "a lift stops the retry");
	expect(!fp5_press_strikes(0), "no scored miss is no strike");
	expect(fp5_press_strikes(1) == 1, "one scored miss is one strike");
	expect(fp5_press_strikes(3) == 1, "three scored misses are one strike");
	expect(fp5_log_is_score(
		       "FtVerifySubTemplate() score = 40, matchCnts = 2"),
	       "score diary");
	expect(fp5_log_keep(
		       "focaltech-lib FtVerifySubTemplate() score = 40, matchCnts = 2"),
	       "score line is kept");
	expect(!fp5_log_keep("0123456789abcdef0123"), "long hex is dropped");

	memset(log, 0, sizeof(log));
	memcpy(log, "interrupt type: 0x212 avgv = 966", 32);
	fp5_scan_log(log, sizeof(log), &sl);
	expect(sl.itype == 0x212 && sl.avgv == 966, "last leftover");
	expect(!fp5_real_down(sl.itype, sl.esd), "scanned leftover");

	memset(log, 0, sizeof(log));
	memcpy(log + 4, "interrupt type: 0x2\nframe raw[0]: avgv = 300", 44);
	fp5_scan_log(log, sizeof(log), &sl);
	expect(sl.itype == 0x2 && sl.avgv == 300 && !sl.esd, "real sample text");
	expect(fp5_real_down(sl.itype, sl.esd) && fp5_score_sample(sl.avgv),
	       "scanned real sample");
}

static void test_gpfile(void)
{
	uint8_t sb[0x200];
	struct fp5_gp gp;

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = 12;
	expect(fp5_gp_decode(sb, sizeof(sb), &gp) == 0 && gp.init12, "op 12");
	fp5_gp_init_reply(sb);
	expect(*(uint64_t *)(sb + 4) == 2, "op 12 stores 2");
	expect(fp5_gp_init_resp_len() == 0xc, "op 12 resp len");

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = (0 << 2) | FP5_GP_UNLINK;
	memcpy(sb + 4, "data/ft_fp_serial_id.bin", 24);
	*(uint32_t *)(sb + 0x104) = 0;
	*(uint32_t *)(sb + 0x108) = 16;
	expect(fp5_gp_decode(sb, sizeof(sb), &gp) == 0, "unlink decode");
	expect(gp.act == FP5_GP_UNLINK, "unlink act");
	expect(fp5_gp_unlink_performs() == 0, "unlink does not delete");
	expect(strcmp(gp.rel, "data/ft_fp_serial_id.bin") == 0, "unlink path");
	expect((fp5_gp_write_oflags() & O_TRUNC) == 0, "write is not O_TRUNC");
	expect((fp5_gp_write_oflags() & (O_RDWR | O_CREAT | O_SYNC)) ==
		       (O_RDWR | O_CREAT | O_SYNC),
	       "write flags");
	expect(fp5_gp_is_template("ff_template_0_1.bin"), "template name");
	expect(!fp5_gp_is_template("ft_fp_serial_id.bin"), "serial is not template");
	{
		static const char hdr[] = "xxxx/ff_template_0_0.bin";
		static const char serial[] = "/data/vendor_de/0/fpdata/ft_fp_serial_id.bin";

		expect(fp5_gp_header_is_template((const uint8_t *)hdr, sizeof(hdr) - 1),
		       "header template path");
		expect(!fp5_gp_header_is_template((const uint8_t *)serial, sizeof(serial) - 1),
		       "header serial is not template");
	}

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = FP5_GP_WRITE;
	memcpy(sb + 4, "../x", 5);
	expect(fp5_gp_decode(sb, sizeof(sb), &gp) != 0, "reject dotdot");
}

static void test_rpmb(void)
{
	uint8_t sb[0x800];
	uint8_t frame[512];
	struct fp5_rpmb plan;

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = 0x101;
	expect(fp5_rpmb_plan(sb, sizeof(sb), &plan) == 0, "provision plan");
	expect(plan.kind == FP5_RPMB_REFUSE, "cmd 0x101 refused");

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = 0x103;
	*(uint32_t *)(sb + 4) = 2;
	*(uint32_t *)(sb + 8) = 512;
	*(uint32_t *)(sb + 0xc) = 0x18;
	put_frame_rr(sb + 0x18, 3, 0);
	put_frame_rr(sb + 0x18 + 512, 3, 0);
	expect(fp5_rpmb_plan(sb, sizeof(sb), &plan) == 0, "multi write plan");
	expect(plan.kind == FP5_RPMB_MULTI_WRITE, "multi write kind");
	expect(plan.off == 0x18 && plan.out_n == 2, "multi write placement");
	expect(plan.persist_on_result0 && !plan.single_rr3_persist, "persist on result 0");
	put_frame_rr(frame, 5, 0);
	frame[504] = 0;
	frame[505] = 0;
	frame[506] = 0x01;
	frame[507] = 0x02;
	fp5_rpmb_place_multi_write(sb, frame);
	expect(*(uint32_t *)(sb + 4) == 0, "write status 0");
	expect(*(uint32_t *)(sb + 8) == 512, "write got 512");
	expect(*(uint32_t *)(sb + 0xc) == 0x14, "write placed at 0x14");
	expect(memcmp(sb + 0x14, frame, 512) == 0, "result frame at 0x14");
	expect(fp5_rpmb_frame_result(sb + 0x14) == 0, "result 0");

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = 0x102;
	*(uint32_t *)(sb + 4) = 1;
	*(uint32_t *)(sb + 8) = 512;
	*(uint32_t *)(sb + 0xc) = 0x18;
	{
		static const uint32_t at[] = { 0x08, 0x10, 0x14, 0x18, 0x20 };
		unsigned i;

		for (i = 0; i < 5; i++) {
			sb[at[i] + 510] = 0x00;
			sb[at[i] + 511] = 0x01;
		}
	}
	expect(fp5_rpmb_plan(sb, sizeof(sb), &plan) == 0, "rr1 plan");
	expect(plan.kind == FP5_RPMB_REFUSE, "req_resp 1 refused");

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = 0x102;
	*(uint32_t *)(sb + 4) = 2;
	*(uint32_t *)(sb + 8) = 512;
	*(uint32_t *)(sb + 0xc) = 0x18;
	put_frame_rr(sb + 0x18, 4, 0);
	put_frame_rr(sb + 0x18 + 512, 4, 0);
	expect(fp5_rpmb_plan(sb, sizeof(sb), &plan) == 0, "multi read plan");
	expect(plan.kind == FP5_RPMB_MULTI_READ && plan.off == 0x18,
	       "read stays at request offset");
}

static void test_time_and_config(void)
{
	uint8_t sb[64];
	char cfg[2048];

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = 0x302;
	fp5_time_apply(sb, sizeof(sb), 5, 0);
	expect(*(uint32_t *)(sb + 4) == 5, "utc sec");

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = 0x999;
	fp5_time_apply(sb, sizeof(sb), 1, 0);
	expect(*(uint32_t *)(sb + 4) == 0xffffffffu, "unknown time");

	memset(sb, 0, sizeof(sb));
	*(uint32_t *)sb = 0x303;
	fp5_time_apply(sb, sizeof(sb), 0, 0);
	expect(*(uint32_t *)(sb + 16) == 1, "mday");
	expect(*(uint32_t *)(sb + 20) == 0, "mon");
	expect(*(uint32_t *)(sb + 24) == 70, "year");

	expect(fp5_sync_config(cfg, sizeof(cfg), 0) > 0, "config");
	expect(strstr(cfg, "\"enable_trusted_enrollment\":false") != NULL, "trusted off");
	expect(strstr(cfg, "\"framework_log_level\":1") != NULL, "log level 1");
	expect(strstr(cfg, "\"firmware_log_level\":3") != NULL, "fw log 3");
	expect(strstr(cfg, "\"algorithm_log_level\":2") != NULL, "algorithm diary 2");
	expect(strstr(cfg, "\"enable_algorithm_log\":true") != NULL,
	       "algorithm diary on");
	expect(strstr(cfg, "\"algorithm_log_level\":3") == NULL,
	       "algorithm diary is not 3");
	expect(strstr(cfg, "\"preferred_device_id\":37777") != NULL,
	       "chip profile 37777");
	expect(strstr(cfg, "37841") == NULL, "unknown chip id is not sent");
	expect(strstr(cfg, "0x2001") == NULL, "config has no HL enroll");
}

/* Short TIME listener buffers: nothing past len may change. */
static void test_time_short_buffer(void)
{
	static const uint32_t cmds[] = { 0x302, 0x303, 0x304, 0x305, 0x306, 0x999 };
	uint8_t buf[96];
	size_t len, k;
	unsigned c;

	for (c = 0; c < sizeof(cmds) / sizeof(cmds[0]); c++) {
		for (len = 48; len < 64; len++) {
			int ok = 1;

			memset(buf, 0xcc, sizeof(buf));
			memcpy(buf, &cmds[c], 4);
			fp5_time_apply(buf, len, 86400 * 365, 7);
			for (k = len; k < sizeof(buf); k++) {
				if (buf[k] != 0xcc)
					ok = 0;
			}
			expect(ok, "time reply stays inside len");
		}
	}
	memset(buf, 0xcc, sizeof(buf));
	*(uint32_t *)buf = 0x302;
	fp5_time_apply(buf, 48, 5, 0);
	expect(*(uint32_t *)(buf + 4) == 5, "short buffer still answered");
	expect(buf[47] == 0 && buf[48] == 0xcc, "short buffer cleared to len only");
	memset(buf, 0xcc, sizeof(buf));
	*(uint32_t *)buf = 0x302;
	fp5_time_apply(buf, 47, 5, 0);
	expect(buf[4] == 0xcc, "under 48 bytes is left alone");
}

/* Copy text to the very end of an exact-size heap buffer and scan it. */
static void scan_tail(const char *text, struct fp5_log *out)
{
	size_t n = strlen(text);
	uint8_t *b = malloc(n);

	if (!b) {
		expect(0, "malloc");
		memset(out, 0, sizeof(*out));
		return;
	}
	memcpy(b, text, n);
	fp5_scan_log(b, n, out);
	free(b);
}

static void test_scan_log_tail(void)
{
	struct fp5_log sl;

	scan_tail("....query event state: [", &sl);
	expect(!sl.esd && !sl.saw_itype && !sl.saw_avgv, "qes needle at end");
	scan_tail("query event state: [es", &sl);
	expect(!sl.esd, "truncated esd tag");
	scan_tail("query event state: [esd]", &sl);
	expect(sl.esd, "esd tag at end");
	scan_tail("query event sta", &sl);
	expect(!sl.esd, "truncated qes needle");
	scan_tail("xx avgv = 123", &sl);
	expect(sl.saw_avgv && sl.avgv == 123, "avgv at end");
	scan_tail("avgv=7", &sl);
	expect(sl.saw_avgv && sl.avgv == 7, "short avgv at end");
	scan_tail("avgv", &sl);
	expect(!sl.saw_avgv, "truncated avgv");
	scan_tail("interrupt type: 0x2", &sl);
	expect(sl.saw_itype && sl.itype == 0x2, "itype at end");
	scan_tail("interrupt type: 0", &sl);
	expect(!sl.saw_itype, "itype cut before x");
	scan_tail("interrupt typ", &sl);
	expect(!sl.saw_itype, "truncated itype needle");
	fp5_scan_log(NULL, 0, &sl);
	expect(!sl.saw_itype && !sl.saw_avgv, "null log");
}

static void test_group(void)
{
	uint8_t g[FP5_GROUP_CAP + 8];
	char path[FP5_GROUP_CAP + 8];
	int n;

	memset(g, 0xcc, sizeof(g));
	n = fp5_build_group(g, FP5_GROUP_CAP, "");
	expect(n == 5, "empty group length");
	expect(g[0] == 0 && g[4] == 0 && g[FP5_GROUP_CAP - 1] == 0,
	       "empty group zeroed");
	expect(g[FP5_GROUP_CAP] == 0xcc, "group stays in cap");

	n = fp5_build_group(g, FP5_GROUP_CAP, "/data/fp");
	expect(n == 4 + 8 + 1 && !memcmp(g + 4, "/data/fp", 9), "group path");

	memset(path, 'a', sizeof(path));
	path[FP5_GROUP_CAP - 5] = 0; /* 123 bytes: the longest that fits */
	memset(g, 0xcc, sizeof(g));
	n = fp5_build_group(g, FP5_GROUP_CAP, path);
	expect(n == FP5_GROUP_CAP, "longest group path fits");
	expect(g[FP5_GROUP_CAP - 1] == 0 && g[FP5_GROUP_CAP] == 0xcc,
	       "longest group path NUL inside cap");

	path[FP5_GROUP_CAP - 5] = 'a';
	path[FP5_GROUP_CAP - 4] = 0; /* 124 bytes */
	memset(g, 0xcc, sizeof(g));
	expect(fp5_build_group(g, FP5_GROUP_CAP, path) == -1, "124-byte path refused");
	expect(g[0] == 0xcc && g[FP5_GROUP_CAP] == 0xcc, "refused path writes nothing");
	path[FP5_GROUP_CAP + 7] = 0;
	expect(fp5_build_group(g, FP5_GROUP_CAP, path) == -1, "long path refused");
	expect(fp5_build_group(g, FP5_GROUP_CAP, NULL) == -1, "null path refused");
	expect(fp5_build_group(g, 4, "") == -1, "tiny cap refused");
}

int main(void)
{
	test_opcodes();
	test_decisions();
	test_gpfile();
	test_rpmb();
	test_time_and_config();
	test_time_short_buffer();
	test_scan_log_tail();
	test_group();
	if (fails) {
		fprintf(stderr, "%d failed\n", fails);
		return 1;
	}
	printf("ok\n");
	return 0;
}
