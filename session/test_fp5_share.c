/*
 * Host harness for the share socket in serve mode (no TEE, no sensor).
 * Builds fp5-qtee-session.c with FP5_SERVE_TEST: set_group is a no-op and
 * auth_once is the fake below. A finger is "put down" by creating the
 * file in FP5_TEST_FINGER. test_fp5_share.py drives it.
 */
#define FP5_SERVE_TEST 1
#define main session_main
#include "fp5-qtee-session.c"
#undef main

static int test_set_group(struct svc *s)
{
	(void)s;
	return 0;
}

static int test_auth_once(struct svc *s, int which)
{
	const char *finger = getenv("FP5_TEST_FINGER");
	int i;

	(void)s;
	fp5_att_begin(&serve_att, 0);
	if (serve_poll_cmds()) {
		logf("AUTH %d cancelled", which);
		return FP5_ATT_CANCELLED;
	}
	logf("AUTH %d arm mode=1 irq=0", which);
	for (i = 0; i < 1000; i++) {
		if (serve_poll_cmds()) {
			logf("AUTH %d cancelled", which);
			return FP5_ATT_CANCELLED;
		}
		if (finger && access(finger, F_OK) == 0) {
			int v;

			unlink(finger);
			logf("AUTH %d REAL DOWN itype=0x2", which);
			v = fp5_att_finish(&serve_att, 1);
			if (v == FP5_ATT_HIT)
				logf("AUTH HIT %d itype=0x2 avgv=900 fid=1979259351 rc=0", which);
			else
				logf("AUTH %d cancelled after match", which);
			return v;
		}
		irq_sleep(10);
	}
	logf("AUTH %d no finger", which);
	return FP5_ATT_NONE;
}

int main(void)
{
	struct svc s;

	setvbuf(stdout, NULL, _IOLBF, 0);
	memset(&s, 0, sizeof(s));
	return serve_loop(&s);
}
