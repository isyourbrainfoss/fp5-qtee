#include "fp5_wire.h"

#include <fcntl.h>
#include <stdio.h>
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
	expect(fp5_score_sample(300), "avgv 300");
	expect(!fp5_score_sample(966), "empty avgv");
	expect(!fp5_score_sample(0), "avgv 0");
	expect(!fp5_score_sample(600), "avgv 600");
	expect(fp5_burst_ok(avg, 3), "three good frames");
	avg[2] = 900;
	expect(!fp5_burst_ok(avg, 3), "one empty frame");
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

	expect(fp5_sync_config(cfg, sizeof(cfg), 0, FP5_DEVICE_ID_DEFAULT) > 0,
	       "config");
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
	expect(strstr(cfg, "\"enable_trustlet_native_log\":false") != NULL,
	       "native log off");

	expect(fp5_sync_config(cfg, sizeof(cfg), 1, 37841) > 0, "config 37841");
	expect(strstr(cfg, "\"preferred_device_id\":37841,") != NULL,
	       "configured chip profile 37841");
	expect(strstr(cfg, "37777") == NULL, "default id not sent when configured");
	expect(strstr(cfg, "\"enable_trustlet_native_log\":true") != NULL,
	       "native log on");
	expect(fp5_sync_config(cfg, 64, 1, 37841) == -1, "short config buffer");
}

static void test_device_id(void)
{
	uint32_t v = 0;
	uint8_t buf[8] = { 0, 0xd1, 0x93, 0, 0x93, 0x91, 0, 0 };

	expect(FP5_DEVICE_ID_DEFAULT == 0x9391, "default is 0x9391");
	expect(fp5_parse_device_id("37841", &v) == 0 && v == 37841, "decimal id");
	expect(fp5_parse_device_id("0x93D1", &v) == 0 && v == 0x93d1, "hex id");
	expect(fp5_parse_device_id("0x9391", &v) == 0 && v == 37777, "hex default");
	expect(fp5_parse_device_id("65535", &v) == 0 && v == 65535, "max id");
	v = 7;
	expect(fp5_parse_device_id("65536", &v) == -1 && v == 7, "over 16 bits");
	expect(fp5_parse_device_id("0", &v) == -1, "zero id");
	expect(fp5_parse_device_id("", &v) == -1, "empty id");
	expect(fp5_parse_device_id("0x", &v) == -1, "bare 0x");
	expect(fp5_parse_device_id("-1", &v) == -1, "negative id");
	expect(fp5_parse_device_id("378x", &v) == -1, "trailing junk");
	expect(fp5_parse_device_id("93D1", &v) == -1, "hex without 0x");
	expect(fp5_parse_device_id(" 37841", &v) == -1, "leading space");
	expect(fp5_parse_device_id("99999999999999999999", &v) == -1, "huge id");
	expect(fp5_parse_device_id(NULL, &v) == -1, "null id");

	expect(fp5_find_u16(buf, sizeof(buf), 0x93d1, 0) == 1, "le16 0x93d1");
	expect(fp5_find_u16(buf, sizeof(buf), 0x9391, 1) == 4, "be16 0x9391");
	expect(fp5_find_u16(buf, sizeof(buf), 0x9391, 0) == -1, "le16 0x9391 absent");
	expect(fp5_find_u16(buf, 2, 0x93d1, 0) == -1, "search stays in n");
}

int main(void)
{
	test_opcodes();
	test_decisions();
	test_gpfile();
	test_rpmb();
	test_time_and_config();
	test_device_id();
	if (fails) {
		fprintf(stderr, "%d failed\n", fails);
		return 1;
	}
	printf("ok\n");
	return 0;
}
