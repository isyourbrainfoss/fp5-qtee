#include "fp5_cmd.h"
#include "fp5_image.h"
#include "fp5_tee.h"
#include "fp5_wire.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <sys/ioctl.h>
#include <scsi/sg.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/*
 * QTEE sendRequest returned MAXDATA (-95) for both 0x78000 and 8K/16K.
 * Stay under 4K per buffer until a larger size is shown to work.
 */
/* 1024/2048 is accepted. 2048/2048 is MAXDATA (-95). Config JSON is 1165 bytes. */
#define REQ_SZ 1536u
#define RSP_SZ 2048u
#define LID_GP 0x7000u
#define LID_RPMB 0x2000u
#define LID_TIME 11u
#define LID_SSD 0x3000u

struct listener {
	uint32_t id;
	struct fp5_shm shm;
};

struct svc {
	struct fp5_tee tee;
	pthread_t thr;
	volatile int ready;
	volatile int stop;
	pthread_mutex_t mu;
	struct listener ls[4];
	uint64_t listener_svc[4];
	int nls;
	int bsg;
	uint32_t last_rpmb_cmd;
	uint16_t last_rpmb_result;
	int last_rpmb_valid;
	int persist_write;
	int template_write;
	char template_name[256];
	unsigned rpmb_n;
	unsigned gp_n;
	uint8_t *req;
	uint8_t *rsp;
	uint8_t *req_out;
	uint8_t *rsp_out;
	uint64_t app;
	struct fp5_log last;
	int32_t last_rc;
	int cap_try;
};

static void sigusr1_nop(int sig)
{
	(void)sig;
}

static void logl(const char *s)
{
	flockfile(stdout);
	fputs(s, stdout);
	fputc('\n', stdout);
	fflush(stdout);
	funlockfile(stdout);
}

static void logf(const char *fmt, ...)
{
	char line[512];
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(line, sizeof(line), fmt, ap);
	va_end(ap);
	logl(line);
}

static int read_whole(const char *path, uint8_t **out, size_t *len)
{
	FILE *f;
	long sz;
	uint8_t *b;
	size_t n;

	f = fopen(path, "rb");
	if (!f)
		return -1;
	if (fseek(f, 0, SEEK_END) != 0) {
		fclose(f);
		return -1;
	}
	sz = ftell(f);
	if (sz <= 0 || sz > 8 * 1024 * 1024) {
		fclose(f);
		return -1;
	}
	rewind(f);
	b = malloc((size_t)sz);
	if (!b) {
		fclose(f);
		return -1;
	}
	n = fread(b, 1, (size_t)sz, f);
	fclose(f);
	if (n != (size_t)sz) {
		free(b);
		return -1;
	}
	*out = b;
	*len = n;
	return 0;
}

static int sysfs_write(const char *path, const char *text)
{
	int fd = open(path, O_WRONLY);
	ssize_t n;

	if (fd < 0)
		return -1;
	n = write(fd, text, strlen(text));
	close(fd);
	return n > 0 ? 0 : -1;
}

#define IRQ_COUNT "/sys/class/focaltech_fp/focaltech_fp/irq_count"

/*
 * serve mode keeps one session open and takes commands on stdin
 * (fp5_cmd.h). A wait for a finger also watches stdin, so "cancel"
 * ends it at once. irq_fd stays open there so the wait can poll it.
 * serve_att holds the running attempt: its irq baseline and whether it
 * was cancelled. stdin is read before every irq check and again after
 * capture, so a cancelled attempt never prints AUTH HIT.
 */
static int serve_on;
static int serve_quit;
static struct fp5_attempt serve_att;
static int irq_fd = -1;
static struct fp5_cmdbuf serve_in;

static unsigned read_irq(void)
{
	char b[32];
	int fd, n;

	if (irq_fd >= 0) {
		/* pread also re-arms sysfs poll for the next change. */
		n = (int)pread(irq_fd, b, sizeof(b) - 1, 0);
	} else {
		fd = open(IRQ_COUNT, O_RDONLY);
		if (fd < 0)
			return 0;
		n = (int)read(fd, b, sizeof(b) - 1);
		close(fd);
	}
	if (n <= 0)
		return 0;
	b[n] = 0;
	return (unsigned)strtoul(b, NULL, 10);
}

/* Next command from stdin. FP5_CMD_NONE when timeout_ms passes first. */
static int serve_read_cmd(int timeout_ms)
{
	char line[FP5_CMD_LINE];
	char chunk[FP5_CMD_LINE];

	for (;;) {
		struct pollfd p = { .fd = 0, .events = POLLIN };
		ssize_t n;
		int r;

		if (fp5_cmd_next(&serve_in, line, sizeof(line)))
			return fp5_cmd_parse(line);
		r = poll(&p, 1, timeout_ms);
		if (r < 0) {
			if (errno == EINTR)
				continue;
			return FP5_CMD_QUIT;
		}
		if (r == 0)
			return FP5_CMD_NONE;
		n = read(0, chunk, sizeof(chunk));
		if (n < 0) {
			if (errno == EINTR || errno == EAGAIN)
				continue;
			return FP5_CMD_QUIT;
		}
		if (n == 0)
			return FP5_CMD_QUIT;	/* the watcher went away */
		fp5_cmd_push(&serve_in, chunk, (size_t)n);
		/* Take any whole line now, but do not block on a partial one. */
		timeout_ms = 0;
	}
}

/*
 * Take every command already waiting on stdin, without blocking, and
 * apply it to the running attempt. An auth here is rejected out loud,
 * not dropped. Returns 1 once the attempt is cancelled.
 */
static int serve_poll_cmds(void)
{
	int guard;

	if (!serve_on)
		return 0;
	for (guard = 0; guard < 64; guard++) {
		int cmd = serve_read_cmd(0);
		int r;

		if (cmd == FP5_CMD_NONE && !memchr(serve_in.buf, '\n', serve_in.n))
			break;
		r = fp5_att_cmd(&serve_att, cmd);
		if (r == FP5_REPLY_BUSY)
			logl("SERVE reject auth busy");
		else if (r == FP5_REPLY_UNKNOWN)
			logl("SERVE reject unknown");
		if (cmd == FP5_CMD_QUIT) {
			serve_quit = 1;
			break;
		}
	}
	return serve_att.cancelled;
}

/*
 * One 20 ms step of a finger wait in serve mode. Returns 1 when the
 * wait should end because of a command. If the driver calls
 * sysfs_notify on irq_count, POLLPRI ends the step early. If it does
 * not, the step simply times out, as usleep did.
 */
static int serve_wait_step(int ms)
{
	struct pollfd p[2];
	int np = 1;

	p[0].fd = 0;
	p[0].events = POLLIN;
	p[0].revents = 0;
	if (irq_fd >= 0) {
		p[1].fd = irq_fd;
		p[1].events = POLLPRI | POLLERR;
		p[1].revents = 0;
		np = 2;
	}
	if (poll(p, (nfds_t)np, ms) > 0 && (p[0].revents & (POLLIN | POLLHUP)))
		return serve_poll_cmds();
	return serve_att.cancelled;
}

static int wait_irq(unsigned *prev, int ms)
{
	while (ms > 0) {
		unsigned now;

		if (serve_on) {
			/* stdin first: a cancel that is already here wins over
			 * an irq change that is also already here. */
			if (serve_poll_cmds())
				return 0;
			now = read_irq();
			if (fp5_att_irq(&serve_att, now)) {
				*prev = now;
				return 1;
			}
			if (serve_wait_step(20))
				return 0;
			ms -= 20;
			continue;
		}
		now = read_irq();
		if (now != *prev) {
			*prev = now;
			return 1;
		}
		usleep(20 * 1000);
		ms -= 20;
	}
	return 0;
}

/* Wait at most slice ms. Subtract the time actually spent from left. */
static int wait_slice(unsigned *prev, int *left_ms, int slice)
{
	struct timespec a, b;
	long used;
	int budget, got;

	if (*left_ms <= 0)
		return 0;
	budget = slice;
	if (budget > *left_ms)
		budget = *left_ms;
	if (budget < 1)
		budget = 1;
	clock_gettime(CLOCK_MONOTONIC, &a);
	got = wait_irq(prev, budget);
	clock_gettime(CLOCK_MONOTONIC, &b);
	used = (b.tv_sec - a.tv_sec) * 1000L +
	       (b.tv_nsec - a.tv_nsec) / 1000000L;
	if (used < 1)
		used = 1;
	if (used >= *left_ms)
		*left_ms = 0;
	else
		*left_ms -= (int)used;
	return got;
}

static int mkdir_p(char *path)
{
	char *p;

	for (p = path + 1; *p; p++) {
		if (*p != '/')
			continue;
		*p = 0;
		if (mkdir(path, 0700) && errno != EEXIST) {
			*p = '/';
			return -1;
		}
		*p = '/';
	}
	return 0;
}

static int mount_persist(void)
{
	FILE *f;
	char line[256];
	int mounted = 0;

	f = fopen("/proc/mounts", "r");
	if (f) {
		while (fgets(line, sizeof(line), f)) {
			if (strstr(line, " /mnt/persist "))
				mounted = 1;
		}
		fclose(f);
	}
	if (mounted)
		return 0;
	mkdir("/mnt/persist", 0755);
	if (mount("/dev/disk/by-partlabel/persist", "/mnt/persist", "ext4",
		  MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) &&
	    errno != EBUSY) {
		logf("persist mount: %s", strerror(errno));
		return -1;
	}
	logl("persist mounted");
	return 0;
}

static int sensor_on(void)
{
	const char *base = "/sys/class/focaltech_fp/focaltech_fp";
	char path[128];
	struct utsname u;
	char ko[256];

	if (access(base, F_OK) != 0) {
		if (uname(&u))
			return -1;
		snprintf(ko, sizeof(ko),
			 "/lib/modules/%s/extra/focaltech_fp_life.ko", u.release);
		if (access(ko, R_OK) != 0) {
			logf("no focaltech_fp_life at %s", ko);
			return -1;
		}
		{
			pid_t pid = fork();

			if (pid == 0) {
				execl("/sbin/insmod", "insmod", ko, "auto_spiclk=1",
				      (char *)NULL);
				execl("/usr/sbin/insmod", "insmod", ko, "auto_spiclk=1",
				      (char *)NULL);
				_exit(127);
			}
			if (pid > 0) {
				int st = 0;

				waitpid(pid, &st, 0);
				logf("insmod focaltech_fp_life status=%d", st);
			}
		}
	}
	snprintf(path, sizeof(path), "%s/vdd", base);
	sysfs_write(path, "1\n");
	snprintf(path, sizeof(path), "%s/spiclk", base);
	sysfs_write(path, "1\n");
	snprintf(path, sizeof(path), "%s/listen", base);
	sysfs_write(path, "1\n");
	if (access(base, F_OK) != 0) {
		logl("focaltech sysfs missing");
		return -1;
	}
	{
		int fd = open("/dev/focaltech_fp", O_RDWR);
		int rc = -1;

		if (fd >= 0) {
			ioctl(fd, _IO('f', 0x07), 0);
			ioctl(fd, _IO('f', 0x05), 0);
			rc = ioctl(fd, _IO('f', 0x02), 0);
			ioctl(fd, _IO('f', 0x03), 0);
			close(fd);
		}
		logf("sensor reset rc=%d irq=%u", rc, read_irq());
		usleep(50 * 1000);
	}
	return 0;
}

static int rpmb_xfer(int fd, void *buf, uint32_t len, int send)
{
	uint8_t cdb[12];
	uint8_t sense[32];
	sg_io_hdr_t io;
	int try;

	if (fd < 0 || len < 512 || len > 4096)
		return -1;
	for (try = 0; try < 4; try++) {
		memset(&io, 0, sizeof(io));
		memset(cdb, 0, sizeof(cdb));
		memset(sense, 0, sizeof(sense));
		cdb[0] = send ? 0xB5 : 0xA2;
		cdb[1] = 0xEC;
		cdb[3] = 0x01;
		cdb[6] = (uint8_t)((len >> 24) & 0xff);
		cdb[7] = (uint8_t)((len >> 16) & 0xff);
		cdb[8] = (uint8_t)((len >> 8) & 0xff);
		cdb[9] = (uint8_t)(len & 0xff);
		io.interface_id = 'S';
		io.dxfer_direction = send ? SG_DXFER_TO_DEV : SG_DXFER_FROM_DEV;
		io.cmd_len = 12;
		io.mx_sb_len = sizeof(sense);
		io.dxfer_len = len;
		io.dxferp = buf;
		io.cmdp = cdb;
		io.sbp = sense;
		io.timeout = 30000;
		if (ioctl(fd, SG_IO, &io) < 0)
			return -1;
		if (!io.status && !io.host_status && !io.driver_status)
			return 0;
		if ((sense[2] & 0x0f) == 6)
			continue;
		return -1;
	}
	return -1;
}

static struct listener *find_ls(struct svc *s, uint64_t id)
{
	int i;

	for (i = 0; i < s->nls; i++) {
		if (s->ls[i].id == (uint32_t)id)
			return &s->ls[i];
	}
	return NULL;
}

static void serve_gp(struct svc *s, uint8_t *sb, size_t len)
{
	struct fp5_gp gp;
	char full[320];
	const char *root;
	int fd, err = 0;
	uint32_t got = 0;

	s->gp_n++;
	if (fp5_gp_decode(sb, len, &gp)) {
		if (len >= 12)
			fp5_gp_reply(sb, 22, 0);
		logf("GPFILE decode fail op=%u", len >= 4 ? *(uint32_t *)sb : 0);
		return;
	}
	if (gp.init12) {
		fp5_gp_init_reply(sb);
		logl("GPFILE op 12 -> 2");
		return;
	}
	root = fp5_gp_root(gp.root);
	snprintf(full, sizeof(full), "/mnt/persist/%s/%s", root, gp.rel);
	if (gp.act == FP5_GP_UNLINK) {
		logf("GPFILE UNLINK '%s'", full);
		if (fp5_gp_unlink_performs())
			unlink(full);
		fp5_gp_reply(sb, 0, 0);
		return;
	}
	if (gp.act == FP5_GP_WRITE) {
		uint32_t nlen = gp.len;
		ssize_t n;

		if (0x110u + nlen > len)
			nlen = len > 0x110 ? (uint32_t)(len - 0x110) : 0;
		mkdir_p(full);
		fd = open(full, fp5_gp_write_oflags(), 0600);
		if (fd < 0) {
			err = errno;
			logf("GPFILE WRITE open '%s' err=%d", full, err);
			fp5_gp_reply(sb, (uint32_t)err, 0);
			return;
		}
		if (gp.off > 0 && lseek(fd, gp.off, SEEK_SET) < 0) {
			err = errno;
			close(fd);
			fp5_gp_reply(sb, (uint32_t)err, 0);
			return;
		}
		n = write(fd, sb + 0x110, nlen);
		close(fd);
		if (n < 0)
			err = errno;
		else
			got = (uint32_t)n;
		logf("GPFILE WRITE '%s' off=%d n=%zd", full, gp.off, n);
		if (n >= 0 && (fp5_gp_is_template(gp.rel) ||
			       (gp.off == 0 &&
				fp5_gp_header_is_template(sb + 0x110, (size_t)n)))) {
			pthread_mutex_lock(&s->mu);
			s->template_write = 1;
			snprintf(s->template_name, sizeof(s->template_name), "%s", gp.rel);
			pthread_mutex_unlock(&s->mu);
		}
		fp5_gp_reply(sb, (uint32_t)err, got);
		return;
	}
	{
		uint32_t nlen = gp.len;
		ssize_t n;

		if (0x00cu + nlen > len)
			nlen = len > 0x00c ? (uint32_t)(len - 0x00c) : 0;
		fd = open(full, O_RDONLY);
		if (fd < 0) {
			fp5_gp_reply(sb, (uint32_t)errno, 0);
			logf("GPFILE READ miss '%s'", full);
			return;
		}
		if (gp.off > 0)
			lseek(fd, gp.off, SEEK_SET);
		n = read(fd, sb + 0x00c, nlen);
		close(fd);
		if (n < 0)
			err = errno;
		else
			got = (uint32_t)n;
		logf("GPFILE READ '%s' n=%zd", full, n);
		fp5_gp_reply(sb, (uint32_t)err, got);
	}
}

static void note_rpmb(struct svc *s, uint32_t cmd, uint16_t result, int valid, int persist)
{
	pthread_mutex_lock(&s->mu);
	s->last_rpmb_cmd = cmd;
	s->last_rpmb_result = result;
	s->last_rpmb_valid = valid;
	if (persist)
		s->persist_write = 1;
	s->rpmb_n++;
	pthread_mutex_unlock(&s->mu);
}

static void serve_rpmb(struct svc *s, uint8_t *sb, size_t len)
{
	struct fp5_rpmb plan;
	uint8_t tmp[4096];
	uint8_t rrq[512];
	uint8_t resp[512];
	int ret;

	if (fp5_rpmb_plan(sb, len, &plan)) {
		fp5_rpmb_reply_err(sb, (uint32_t)-EIO);
		return;
	}
	if (plan.kind == FP5_RPMB_REFUSE) {
		if (plan.cmd == 0x101) {
			logl("RPMB refuse PROVISION (key program)");
			fp5_rpmb_reply_err(sb, (uint32_t)(-EPERM));
		} else {
			logl("RPMB refuse key-program req_resp=1");
			fp5_rpmb_reply_err(sb, (uint32_t)(-EIO));
		}
		note_rpmb(s, plan.cmd, 0xffff, 0, 0);
		return;
	}
	if (plan.kind == FP5_RPMB_GET_INFO) {
		uint32_t wc = 0;

		fp5_rpmb_get_wc_frame(tmp);
		ret = rpmb_xfer(s->bsg, tmp, 512, 1);
		if (!ret)
			ret = rpmb_xfer(s->bsg, tmp, 512, 0);
		if (ret) {
			fp5_rpmb_reply_err(sb, (uint32_t)(-EIO));
			logf("RPMB GET_INFO xfer fail");
			note_rpmb(s, 0x104, 0xffff, 0, 0);
			return;
		}
		wc = fp5_rpmb_frame_wc(tmp);
		fp5_rpmb_fill_info(sb, wc);
		logf("RPMB GET_INFO wc=%u result=0x%x", wc, fp5_rpmb_frame_result(tmp));
		note_rpmb(s, 0x104, fp5_rpmb_frame_result(tmp), 1, 0);
		return;
	}
	if (plan.kind == FP5_RPMB_FAIL) {
		fp5_rpmb_reply_err(sb, (uint32_t)(-EIO));
		logf("RPMB cmd=0x%x no frame", plan.cmd);
		note_rpmb(s, plan.cmd, 0xffff, 0, 0);
		return;
	}
	fp5_rpmb_arm_frame(sb + plan.off, plan.cmd);
	if (plan.kind == FP5_RPMB_MULTI_WRITE) {
		uint32_t nbytes = plan.out_n * 512;
		uint16_t result;

		ret = rpmb_xfer(s->bsg, sb + plan.off, nbytes, 1);
		if (!ret) {
			fp5_rpmb_result_read_frame(rrq);
			ret = rpmb_xfer(s->bsg, rrq, 512, 1);
		}
		if (!ret)
			ret = rpmb_xfer(s->bsg, resp, 512, 0);
		if (ret) {
			fp5_rpmb_reply_err(sb, (uint32_t)(-EIO));
			logf("RPMB cmd=0x%x multi-write xfer fail", plan.cmd);
			note_rpmb(s, plan.cmd, 0xffff, 0, 0);
			return;
		}
		fp5_rpmb_place_multi_write(sb, resp);
		result = fp5_rpmb_frame_result(resp);
		logf("RPMB cmd=0x%x result=0x%x wc=%u", plan.cmd, result,
		     fp5_rpmb_frame_wc(resp));
		note_rpmb(s, plan.cmd, result, 1,
			  plan.persist_on_result0 && result == 0);
		return;
	}
	if (plan.single_rr3_persist) {
		pthread_mutex_lock(&s->mu);
		s->persist_write = 1;
		pthread_mutex_unlock(&s->mu);
	}
	ret = rpmb_xfer(s->bsg, sb + plan.off, 512, 1);
	if (ret) {
		fp5_rpmb_reply_err(sb, (uint32_t)(-EIO));
		logf("RPMB cmd=0x%x OUT fail", plan.cmd);
		note_rpmb(s, plan.cmd, 0xffff, 0, 0);
		return;
	}
	if (plan.kind == FP5_RPMB_MULTI_READ) {
		uint32_t nbytes = plan.out_n * 512;

		if (nbytes > sizeof(tmp))
			nbytes = sizeof(tmp);
		ret = rpmb_xfer(s->bsg, tmp, nbytes, 0);
		if (ret) {
			fp5_rpmb_reply_err(sb, (uint32_t)(-EIO));
			note_rpmb(s, plan.cmd, 0xffff, 0, 0);
			return;
		}
		fp5_rpmb_place_multi_read(sb, plan.off, tmp, nbytes);
		logf("RPMB cmd=0x%x result=0x%x read n=%u off=0x%x", plan.cmd,
		     fp5_rpmb_frame_result(tmp), nbytes, plan.off);
		note_rpmb(s, plan.cmd, fp5_rpmb_frame_result(tmp), 1, 0);
		return;
	}
	ret = rpmb_xfer(s->bsg, sb + plan.off, 512, 0);
	if (ret) {
		fp5_rpmb_reply_err(sb, (uint32_t)(-EIO));
		note_rpmb(s, plan.cmd, 0xffff, 0, 0);
		return;
	}
	fp5_rpmb_place_single_ok(sb);
	logf("RPMB cmd=0x%x result=0x%x", plan.cmd,
	     fp5_rpmb_frame_result(sb + plan.off));
	note_rpmb(s, plan.cmd, fp5_rpmb_frame_result(sb + plan.off), 1, 0);
}

static void serve_one(struct svc *s, uint64_t id)
{
	struct listener *ls = find_ls(s, id);
	struct timespec ts;
	uint8_t *sb;

	if (!ls || !ls->shm.mem)
		return;
	sb = ls->shm.mem;
	if (id == LID_GP)
		serve_gp(s, sb, ls->shm.size);
	else if (id == LID_RPMB)
		serve_rpmb(s, sb, ls->shm.size);
	else if (id == LID_TIME) {
		clock_gettime(CLOCK_REALTIME, &ts);
		fp5_time_apply(sb, ls->shm.size, (int64_t)ts.tv_sec, (int32_t)ts.tv_nsec);
		logf("TIME cmd served");
	} else if (id == LID_SSD) {
		fp5_ssd_apply(sb, ls->shm.size);
		logl("SSD status 0");
	}
}

static void *supp_main(void *arg)
{
	struct svc *s = arg;
	uint8_t *ubuf;
	uint8_t *obuf[8] = { 0 };

	ubuf = calloc(1, 65536);
	if (!ubuf) {
		s->ready = 1;
		return NULL;
	}
	s->ready = 1;
	while (!s->stop) {
		struct fp5_supp_req req;
		uint32_t i, n;
		uint32_t req_id;

		if (fp5_tee_supp_recv(&s->tee, ubuf, 65536, &req)) {
			if (errno == EINTR && !s->stop)
				continue;
			if (!s->stop)
				logf("supp recv: %s", strerror(errno));
			break;
		}
		req_id = req.req_id;
		n = req.num_params ? req.num_params : 1;
		if (n > 8)
			n = 8;
		req.params[0].attr = 2u | 0x100u;
		req.params[0].a = req_id;
		req.params[0].b = 0;
		req.params[0].c = 0;
		for (i = 1; i < n; i++) {
			uint32_t ty = (uint32_t)(req.params[i].attr & 0xffu);

			logf("supp p%u ty=%u b=%llu", i, ty,
			     (unsigned long long)req.params[i].b);
			if (ty == 8 && req.params[i].a && req.params[i].b) {
				const uint8_t *ib = (const uint8_t *)(uintptr_t)req.params[i].a;
				size_t nb = req.params[i].b > 12 ? 12 : (size_t)req.params[i].b;
				char hex[40];
				size_t k;

				hex[0] = 0;
				for (k = 0; k < nb; k++)
					snprintf(hex + k * 3, 4, "%02x ", ib[k]);
				logf("supp p%u ib %s", i, hex);
			}
			if (ty == 9 || ty == 10) {
				size_t want = (size_t)req.params[i].b;

				if (want > 65536)
					want = 65536;
				free(obuf[i]);
				obuf[i] = calloc(1, want ? want : 1);
				req.params[i].a = (uint64_t)(uintptr_t)obuf[i];
				req.params[i].b = want;
			} else if (ty == 12 || ty == 13) {
				req.params[i].a = (uint64_t)-1;
				req.params[i].b = 0;
			}
		}
		if (req.func == 0xffffu) {
			/* select() already freed this ureq. SUPPL_SEND is EINVAL. */
			logf("supp release obj=%llu",
			     (unsigned long long)req.object_id);
			continue;
		}
		{
			struct listener *ls = find_ls(s, req.object_id);
			uint32_t w0 = 0;

			if (ls && ls->shm.mem && ls->shm.size >= 4)
				memcpy(&w0, ls->shm.mem, 4);
			logf("supp obj=%llu op=%u n=%u shm0=0x%x",
			     (unsigned long long)req.object_id, req.func, n, w0);
			serve_one(s, req.object_id);
			/*
			 * Command 12's answer is 12 bytes (opcode plus the u64).
			 * A full-size zero output buffer hides that from QTEE.
			 */
			if (ls && ls->id == LID_GP && w0 == 12 && ls->shm.mem) {
				size_t resp = fp5_gp_init_resp_len();
				int published = 0;

				if (resp > ls->shm.size)
					resp = ls->shm.size;
				for (i = 1; i < n; i++) {
					uint32_t ty = (uint32_t)(req.params[i].attr & 0xffu);
					uint8_t *dst;
					size_t cap;

					if (ty != 9 && ty != 10)
						continue;
					dst = (uint8_t *)(uintptr_t)req.params[i].a;
					cap = (size_t)req.params[i].b;
					if (!dst || cap == 0)
						continue;
					if (cap >= resp) {
						memcpy(dst, ls->shm.mem, resp);
						if (cap > resp)
							memset(dst + resp, 0, cap - resp);
						req.params[i].b = resp;
						logf("supp ob%u resp=%zu cap=%zu", i, resp, cap);
					} else {
						/* Short output is a status word, not the opcode. */
						memset(dst, 0, cap);
						logf("supp ob%u status0 cap=%zu", i, cap);
					}
					published = 1;
				}
				if (!published)
					logl("supp op12 no output buffer");
			}
		}
		if (fp5_tee_supp_send(&s->tee, 0, req.params, n)) {
			logf("supp send: %s", strerror(errno));
			break;
		}
	}
	for (int i = 0; i < 8; i++)
		free(obuf[i]);
	free(ubuf);
	return NULL;
}

static int hex_run(const char *s)
{
	int run = 0;

	for (; *s; s++) {
		int hex = (*s >= '0' && *s <= '9') || (*s >= 'a' && *s <= 'f') ||
			  (*s >= 'A' && *s <= 'F');

		if (hex) {
			if (++run >= 20)
				return 1;
		} else {
			run = 0;
		}
	}
	return 0;
}

static int interesting(const char *s)
{
	static const char *keys[] = {
		"interrupt type", "avgv", "frame raw", "ff_file_open", " leave.",
		"SFS_ERROR", "qsee_sfs_open", "ft_fp_serial", "ff_template_",
		"auth success", "identify", "new fid", "enrolled", "error at",
		"enrollment", "template", "challenge", "Base update",
		"Null pointer", "shared memory", "sensor raw", "image buffer",
		"Out of memory", "interrupt", NULL
	};
	int i;

	if (strstr(s, "serial is") || hex_run(s))
		return 0;
	for (i = 0; keys[i]; i++) {
		if (strstr(s, keys[i]))
			return 1;
	}
	return 0;
}

static void print_marks(const char *tag, const uint8_t *buf, size_t n)
{
	size_t i = 0;

	while (i < n) {
		size_t j = i;
		char line[160];

		while (j < n && j - i < 140) {
			unsigned char c = buf[j];

			if (c == '\n' || c == 0 || c < 0x20 || c >= 0x7f)
				break;
			j++;
		}
		if (j - i >= 12) {
			memcpy(line, buf + i, j - i);
			line[j - i] = 0;
			if (interesting(line))
				logf("%s ta: %s", tag, line);
		}
		i = (j > i) ? j + 1 : i + 1;
	}
}

static void dump_runs(const char *tag, const uint8_t *buf, size_t n)
{
	size_t i = 0, shown = 0, k;
	unsigned nz = 0;

	for (k = 0; k < n; k++) {
		if (buf[k])
			nz++;
	}
	logf("%s nz=%u/%zu", tag, nz, n);
	while (i < n && shown < 20) {
		size_t j = i;
		char line[120];

		while (j < n && j - i < 100) {
			unsigned char c = buf[j];

			if (c < 0x20 || c >= 0x7f)
				break;
			j++;
		}
		if (j - i >= 16) {
			memcpy(line, buf + i, j - i);
			line[j - i] = 0;
			if (!strstr(line, "identify") && !strstr(line, "focaltech") &&
			    !strstr(line, "framework_log") &&
			    !strstr(line, "enrolling_overlap")) {
				logf("%s str@%zu %s", tag, i, line);
				shown++;
			}
		}
		i = (j > i) ? j + 1 : i + 1;
	}
}

static void qsee_log_apply(const char *tag, struct fp5_log *dst);
static void qsee_dump_delta(const char *tag);

static int send_cmd(struct svc *s, uint32_t cmd, uint32_t plen, const void *pay,
		    const char *tag)
{
	struct tee_param_view p[10];
	struct fp5_shm capmem;
	uint32_t qret = 0, is64 = 0, i, cap_off = FP5_PAY_OFF;
	int rc, have_cap = 0;

	memset(&capmem, 0, sizeof(capmem));
	capmem.fd = -1;

	if ((uint64_t)FP5_PAY_OFF + plen > REQ_SZ) {
		logf("%s plen %u does not fit", tag, plen);
		return -1;
	}
	fp5_req_set(s->req, cmd, plen);
	if (pay)
		fp5_req_pay(s->req, pay, plen);
	memset(s->rsp, 0, RSP_SZ);
	memset(s->req_out, 0, REQ_SZ);
	memset(s->rsp_out, 0, RSP_SZ);
	memset(p, 0, sizeof(p));
	p[0].attr = 8;
	p[0].a = (uint64_t)(uintptr_t)s->req;
	p[0].b = REQ_SZ;
	p[1].attr = 8;
	p[1].a = (uint64_t)(uintptr_t)s->rsp;
	p[1].b = RSP_SZ;
	p[2].attr = 8;
	p[3].attr = 8;
	p[3].a = (uint64_t)(uintptr_t)&is64;
	p[3].b = sizeof(is64);
	p[4].attr = 9;
	p[4].a = (uint64_t)(uintptr_t)s->req_out;
	p[4].b = REQ_SZ;
	p[5].attr = 9;
	p[5].a = (uint64_t)(uintptr_t)s->rsp_out;
	p[5].b = RSP_SZ;
	for (i = 6; i < 10; i++) {
		p[i].attr = 11;
		p[i].a = (uint64_t)-1;
	}
	/*
	 * focal64 names a capture region at request+0x10. focal32's HAL
	 * leaves that word 0 (enable_hw_reset). Try it only when cap_try
	 * is set, after the 32 KiB shm bridge exists. A rejected region
	 * must not be attached on the enroll path.
	 */
	/* cap_try: 1 offset+object, 2 object only, 3 offset only. */
	if (s->cap_try && cmd == fp5_op_capture()) {
		if (s->cap_try == 1 || s->cap_try == 2) {
			if (fp5_tee_shm_alloc(&s->tee, 32768, &capmem)) {
				logf("%s capture shm: %s", tag, strerror(errno));
				return -1;
			}
			memset(capmem.mem, 0, capmem.size);
			have_cap = 1;
			p[6].a = capmem.id;
			p[6].b = FP5_OBJ_MEM;
			logf("%s capmem id=%u bytes=%zu", tag, capmem.id, capmem.size);
		}
		if (s->cap_try == 1 || s->cap_try == 3) {
			p[2].a = (uint64_t)(uintptr_t)&cap_off;
			p[2].b = sizeof(cap_off);
			logf("%s embed off=0x%x", tag, cap_off);
		}
	}
	rc = fp5_tee_invoke(&s->tee, s->app, 0, p, 10, &qret);
	if (rc || qret) {
		logf("%s invoke rc=%d qret=0x%x errno=%d", tag, rc, qret, errno);
		if (have_cap)
			fp5_tee_shm_free(&capmem);
		qsee_log_apply(tag, &s->last);
		qsee_dump_delta(tag);
		return -1;
	}
	if (have_cap) {
		uint8_t *b = capmem.mem;
		size_t k, nz = 0;

		for (k = 0; k < capmem.size; k++) {
			if (b[k])
				nz++;
		}
		logf("%s capmem nz=%zu/%zu", tag, nz, capmem.size);
		fp5_tee_shm_free(&capmem);
	}
	memcpy(s->req, s->req_out, REQ_SZ);
	fp5_scan_log(s->rsp_out, RSP_SZ, &s->last);
	if (!s->last.saw_itype) {
		struct fp5_log reqlog;

		fp5_scan_log(s->req, REQ_SZ, &reqlog);
		if (reqlog.saw_itype) {
			s->last.itype = reqlog.itype;
			s->last.saw_itype = 1;
			s->last.esd = reqlog.esd;
		}
		if (reqlog.saw_avgv && !s->last.saw_avgv) {
			s->last.avgv = reqlog.avgv;
			s->last.saw_avgv = 1;
		}
	}
	qsee_log_apply(tag, &s->last);
	s->last_rc = fp5_req_rc(s->req);
	logf("%s cmd=0x%x rc=%d itype=0x%x esd=%d avgv=%u req_b=%llu rsp_b=%llu",
	     tag, cmd, (int)s->last_rc, s->last.itype, s->last.esd, s->last.avgv,
	     (unsigned long long)p[4].b, (unsigned long long)p[5].b);
	print_marks(tag, s->rsp_out, RSP_SZ);
	print_marks(tag, s->req, REQ_SZ);
	return 0;
}

/*
 * QSEE REGISTER_LOG_BUFFER lives in the kernel as /dev/qsee_log.
 * The trustlet writes "avgv =" and, on query, "interrupt type" there.
 * The ring cursor is the first two little-endian words. Score only the
 * bytes written since the previous command so an older sample cannot
 * satisfy this one.
 */
#define QSEE_LOG_CAP (128u * 1024u)
#define QSEE_LOG_HDR 8u

static uint8_t *qsee_buf;
static uint8_t *qsee_delta;
static uint32_t qsee_wrap, qsee_off;
static size_t qsee_len;
static size_t qsee_delta_len;
static int qsee_armed;
static int qsee_log_warned;
#define WATCH_DIR "/tmp/fp5-qtee"
static int watch_stop;
static int watch_on;
static pthread_t watch_thr;

static void watch_keep(const char *line, FILE *out)
{
	int hex = 0, k;

	if (strlen(line) < 24)
		return;
	if (strstr(line, "sn num") || strstr(line, "fullduplex") ||
	    strstr(line, "ISPI"))
		return;
	for (k = 0; line[k]; k++) {
		char c = line[k];

		if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
		    (c >= 'A' && c <= 'F'))
			hex++;
		else
			hex = 0;
		if (hex >= 20)
			return;
	}
	if (!strstr(line, "device.c") && !strstr(line, "file.c") &&
	    !strstr(line, "serial") && !strstr(line, "efc") &&
	    !strstr(line, "error at") && !strstr(line, "Null") &&
	    !strstr(line, "exist") && !strstr(line, "SFS") &&
	    !strstr(line, "template") && !strstr(line, "qsee_sfs") &&
	    !strstr(line, "ff_file") && !strstr(line, "ff_device") &&
	    !strstr(line, "initializing") && !strstr(line, "replaced"))
		return;
	fprintf(out, "%s\n", line);
	fflush(out);
}

static void *qsee_watch_main(void *arg)
{
	uint8_t *buf;
	uint32_t off = 0, wrap = 0;
	int armed = 0;
	int fd;
	FILE *out = NULL;
	(void)arg;

	buf = calloc(1, 65536);
	if (!buf) {
		logl("qsee watch: out of memory");
		return NULL;
	}
	/* 0755, not 0700: the app looks for the session binary in this
	 * directory as the desktop user, and this runs as root. */
	if (mkdir(WATCH_DIR, 0755) && errno != EEXIST) {
		logf("qsee watch: mkdir %s: %s", WATCH_DIR, strerror(errno));
		free(buf);
		return NULL;
	}
	/* Root writes here, so do not follow a planted symlink in /tmp. */
	fd = open(WATCH_DIR "/watch.txt",
		  O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0644);
	if (fd >= 0)
		out = fdopen(fd, "w");
	if (!out) {
		logf("qsee watch: open %s/watch.txt: %s", WATCH_DIR, strerror(errno));
		if (fd >= 0)
			close(fd);
		free(buf);
		return NULL;
	}
	while (!watch_stop) {
		int fd;
		ssize_t n;
		uint32_t w, o;

		fd = open("/dev/qsee_log", O_RDONLY);
		if (fd < 0) {
			usleep(50 * 1000);
			continue;
		}
		n = read(fd, buf, 65536);
		close(fd);
		if (n < 8) {
			usleep(30 * 1000);
			continue;
		}
		memcpy(&w, buf, 4);
		memcpy(&o, buf + 4, 4);
		if (!armed) {
			armed = 1;
			wrap = w;
			off = o;
		} else if (w != wrap || o != off) {
			size_t a = off < (size_t)n ? off : 8;
			size_t b = o < (size_t)n ? o : (size_t)n;
			size_t i = 0;
			uint8_t *delta = calloc(1, 65536);
			size_t dn = 0;

			if (delta) {
				if (b >= a) {
					dn = b - a;
					memcpy(delta, buf + a, dn);
				} else {
					if (a < (size_t)n) {
						dn = (size_t)n - a;
						memcpy(delta, buf + a, dn);
					}
					if (b > 8) {
						memcpy(delta + dn, buf + 8, b - 8);
						dn += b - 8;
					}
				}
				while (i < dn) {
					size_t s, e;
					char line[200];

					while (i < dn && (delta[i] < 32 || delta[i] >= 127))
						i++;
					s = i;
					while (i < dn && delta[i] >= 32 && delta[i] < 127)
						i++;
					e = i - s;
					if (e < 24)
						continue;
					if (e > sizeof(line) - 1)
						e = sizeof(line) - 1;
					memcpy(line, delta + s, e);
					line[e] = 0;
					watch_keep(line, out);
				}
				free(delta);
			}
			wrap = w;
			off = o;
		}
		usleep(30 * 1000);
	}
	fclose(out);
	free(buf);
	return NULL;
}

static ssize_t qsee_log_load(void)
{
	int fd;
	ssize_t n;

	if (!qsee_buf || !qsee_delta) {
		/* Both or neither: qsee_copy_new writes qsee_delta whenever
		 * qsee_buf is set. */
		uint8_t *b = calloc(1, QSEE_LOG_CAP);
		uint8_t *d = calloc(1, QSEE_LOG_CAP);

		if (!b || !d) {
			free(b);
			free(d);
			return -1;
		}
		free(qsee_buf);
		free(qsee_delta);
		qsee_buf = b;
		qsee_delta = d;
	}
	fd = open("/dev/qsee_log", O_RDONLY);
	if (fd < 0) {
		if (!qsee_log_warned) {
			logf("qsee_log open: %s", strerror(errno));
			qsee_log_warned = 1;
		}
		return -1;
	}
	n = read(fd, qsee_buf, QSEE_LOG_CAP);
	close(fd);
	if (n > 0)
		qsee_len = (size_t)n;
	return n;
}

static size_t qsee_copy_new(size_t n, uint32_t old_off, uint32_t new_off)
{
	size_t w = 0;

	if (old_off > n)
		old_off = 0;
	if (new_off > n)
		new_off = 0;
	if (new_off >= old_off) {
		w = new_off - old_off;
		if (w > QSEE_LOG_CAP)
			w = QSEE_LOG_CAP;
		memcpy(qsee_delta, qsee_buf + old_off, w);
		return w;
	}
	if (old_off < n) {
		w = n - old_off;
		if (w > QSEE_LOG_CAP)
			w = QSEE_LOG_CAP;
		memcpy(qsee_delta, qsee_buf + old_off, w);
	}
	if (new_off > QSEE_LOG_HDR && w < QSEE_LOG_CAP) {
		size_t len = new_off - QSEE_LOG_HDR;

		if (w + len > QSEE_LOG_CAP)
			len = QSEE_LOG_CAP - w;
		memcpy(qsee_delta + w, qsee_buf + QSEE_LOG_HDR, len);
		w += len;
	}
	return w;
}

static void qsee_log_apply(const char *tag, struct fp5_log *dst)
{
	ssize_t n;
	uint32_t wrap = 0, off = 0, prev_w, prev_o;
	size_t bytes = 0;
	struct fp5_log got;
	struct fp5_log full;

	memset(&got, 0, sizeof(got));
	memset(&full, 0, sizeof(full));
	n = qsee_log_load();
	if (n < (ssize_t)QSEE_LOG_HDR)
		return;
	memcpy(&wrap, qsee_buf, 4);
	memcpy(&off, qsee_buf + 4, 4);
	fp5_scan_log(qsee_buf, (size_t)n, &full);
	prev_w = qsee_wrap;
	prev_o = qsee_off;
	if (!qsee_armed) {
		qsee_wrap = wrap;
		qsee_off = off;
		qsee_armed = 1;
		logf("%s qsee baseline wrap=%u off=%u full_avg=%u full_ity=0x%x",
		     tag, wrap, off, full.avgv, full.itype);
		return;
	}
	if (wrap != prev_w || off != prev_o)
		bytes = qsee_copy_new((size_t)n, prev_o, off);
	qsee_delta_len = bytes;
	qsee_wrap = wrap;
	qsee_off = off;
	if (bytes)
		fp5_scan_log(qsee_delta, bytes, &got);
	if (got.saw_itype) {
		dst->itype = got.itype;
		dst->saw_itype = 1;
		dst->esd = got.esd;
	}
	if (got.saw_avgv) {
		dst->avgv = got.avgv;
		dst->saw_avgv = 1;
	}
	logf("%s qsee off %u->%u bytes=%zu d_avg=%u saw_avg=%d d_ity=0x%x saw_ity=%d full_avg=%u full_ity=0x%x",
	     tag, prev_o, off, bytes, got.avgv, got.saw_avgv, got.itype,
	     got.saw_itype, full.avgv, full.itype);
}

static void qsee_dump_delta(const char *tag)
{
	size_t i = 0, shown = 0;
	int printed = 0;
	char last[180];

	last[0] = 0;
	if (!qsee_delta || !qsee_delta_len) {
		logf("%s qsee text: none", tag);
		return;
	}
	while (i < qsee_delta_len) {
		size_t a, run;
		char line[180];
		int score;

		while (i < qsee_delta_len &&
		       (qsee_delta[i] < 32 || qsee_delta[i] >= 127))
			i++;
		a = i;
		while (i < qsee_delta_len &&
		       qsee_delta[i] >= 32 && qsee_delta[i] < 127)
			i++;
		run = i - a;
		if (run < 12)
			continue;
		if (run > sizeof(line) - 1)
			run = sizeof(line) - 1;
		memcpy(line, qsee_delta + a, run);
		line[run] = 0;
		memcpy(last, line, run + 1);
		score = fp5_log_is_score(line);
		if (!score && shown >= 40)
			continue;
		if (!fp5_log_keep(line))
			continue;
		logf("%s qsee: %s", tag, line);
		printed++;
		if (!score)
			shown++;
	}
	if (!printed && last[0])
		logf("%s qsee last: %s", tag, last);
}

static int buf_has(const uint8_t *buf, size_t n, const char *s)
{
	return memmem(buf, n, s, strlen(s)) != NULL;
}

static void note_serial(struct svc *s)
{
	int phrase = memmem(s->rsp_out, RSP_SZ, "qsee_sfs_open", 13) != NULL ||
		     memmem(s->req, REQ_SZ, "qsee_sfs_open", 13) != NULL ||
		     (qsee_len && memmem(qsee_buf, qsee_len, "qsee_sfs_open", 13));
	int ff_open = memmem(s->rsp_out, RSP_SZ, "ff_file_open", 12) != NULL ||
		      memmem(s->req, REQ_SZ, "ff_file_open", 12) != NULL ||
		      (qsee_len && memmem(qsee_buf, qsee_len, "ff_file_open", 12));
	int err = memmem(s->rsp_out, RSP_SZ, "SFS_ERROR", 9) != NULL ||
		  memmem(s->req, REQ_SZ, "SFS_ERROR", 9) != NULL ||
		  (qsee_len && memmem(qsee_buf, qsee_len, "SFS_ERROR", 9));

	logf("qsee_sfs_open phrase: %s", phrase ? "present" : "absent");
	logf("ff_file_open phrase: %s", ff_open ? "present" : "absent");
	logf("module_serial phrase: %s",
	     (qsee_len && buf_has(qsee_buf, qsee_len, "__module_serial_sync")) ?
	     "present" : "absent");
	logf("not_exist phrase: %s",
	     (qsee_len && buf_has(qsee_buf, qsee_len, "not exist")) ?
	     "present" : "absent");
	logf("null_pointer phrase: %s",
	     (qsee_len && buf_has(qsee_buf, qsee_len, "Null pointer")) ?
	     "present" : "absent");
	logf("efc phrase: %s",
	     (qsee_len && buf_has(qsee_buf, qsee_len, "efc data")) ?
	     "present" : "absent");
	logf("init_chip phrase: %s",
	     (qsee_len && buf_has(qsee_buf, qsee_len, "init_chip")) ?
	     "present" : "absent");
	logf("get_serial phrase: %s",
	     (qsee_len && buf_has(qsee_buf, qsee_len, "get serial from chip")) ?
	     "present" : "absent");
	logf("synchronized phrase: %s",
	     (qsee_len && buf_has(qsee_buf, qsee_len, "synchronized")) ?
	     "present" : "absent");
	logf("initializing phrase: %s",
	     (qsee_len && buf_has(qsee_buf, qsee_len, "initializing the device")) ?
	     "present" : "absent");
	logf("replaced phrase: %s",
	     (qsee_len && buf_has(qsee_buf, qsee_len, "module has been replaced")) ?
	     "present" : "absent");
	if (qsee_delta && qsee_delta_len) {
		size_t i = 0, shown = 0;

		logf("INIT_DEV qsee delta=%zu", qsee_delta_len);
		while (i < qsee_delta_len && shown < 120) {
			size_t a, b, run;
			char line[180];
			int hex = 0, k;

			while (i < qsee_delta_len &&
			       (qsee_delta[i] < 32 || qsee_delta[i] >= 127))
				i++;
			a = i;
			while (i < qsee_delta_len &&
			       qsee_delta[i] >= 32 && qsee_delta[i] < 127)
				i++;
			b = i;
			run = b - a;
			if (run < 24)
				continue;
			if (run > sizeof(line) - 1)
				run = sizeof(line) - 1;
			memcpy(line, qsee_delta + a, run);
			line[run] = '\0';
			for (k = 0; line[k]; k++) {
				char c = line[k];

				if ((c >= '0' && c <= '9') ||
				    (c >= 'a' && c <= 'f') ||
				    (c >= 'A' && c <= 'F'))
					hex++;
				else
					hex = 0;
				if (hex >= 20)
					break;
			}
			if (hex >= 20 || strstr(line, "sn num"))
				continue;
			logf("INIT_DEV qsee: %s", line);
			shown++;
		}
	}
	dump_runs("INIT_DEV rsp", s->rsp_out, RSP_SZ);
	dump_runs("INIT_DEV req", s->req, REQ_SZ);
	if (err)
		logl("SFS_ERROR present");
	print_marks("INIT_DEV", s->rsp_out, RSP_SZ);
	print_marks("INIT_DEV", s->req, REQ_SZ);
}

static int reg_listener(struct svc *s, uint64_t env, uint32_t id, size_t sz)
{
	struct listener *ls;
	struct tee_param_view p[3];
	uint64_t sobj = 0;
	uint32_t qret = 0, lid = id;

	if (s->nls >= 4)
		return -1;
	ls = &s->ls[s->nls];
	if (fp5_tee_open_service(&s->tee, env, 87, &sobj, &qret)) {
		logf("listener svc 87 qret=%u errno=%d", qret, errno);
		return -1;
	}
	if (fp5_tee_shm_alloc(&s->tee, sz, &ls->shm)) {
		logf("listener 0x%x shm: %s", id, strerror(errno));
		return -1;
	}
	memset(ls->shm.mem, 0, ls->shm.size);
	ls->id = id;
	memset(p, 0, sizeof(p));
	p[0].attr = 8;
	p[0].a = (uint64_t)(uintptr_t)&lid;
	p[0].b = sizeof(lid);
	p[1].attr = 11;
	p[1].a = id;
	p[1].b = FP5_OBJ_USER;
	p[2].attr = 11;
	p[2].a = ls->shm.id;
	p[2].b = FP5_OBJ_MEM;
	if (fp5_tee_invoke(&s->tee, sobj, 0, p, 3, &qret)) {
		logf("listener 0x%x invoke errno=%d qret=%u", id, errno, qret);
		fp5_tee_shm_free(&ls->shm);
		return -1;
	}
	logf("listener 0x%x bytes=%zu qret=%u", id, ls->shm.size, qret);
	if (qret && qret != 99) {
		/* The next registration reuses this slot. Do not leak the map. */
		fp5_tee_shm_free(&ls->shm);
		return -1;
	}
	/* Releasing this service object drops the callback. Keep it. */
	s->listener_svc[s->nls] = sobj;
	s->nls++;
	return 0;
}

static const char *verdict_name(uint32_t r)
{
	switch (r) {
	case 0: return "success";
	case 11: return "PIL_ROLLBACK";
	case 12: return "ELF_SIGNATURE";
	case 13: return "METADATA_INVALID";
	case 16: return "ALREADY_LOADED";
	case 28: return "ELF_LOADING";
	default: return "";
	}
}

static int load_once(struct svc *s, uint64_t loader, const char *dir, uint64_t *app,
		     uint32_t *qret_out)
{
	char path[256];
	uint8_t *mdt = NULL, *img = NULL, *raw[8] = { 0 };
	size_t mdt_len = 0, img_len = 0, raw_len[8] = { 0 };
	struct fp5_seg segs[8];
	char dist[128];
	uint32_t qret = 0;
	int i, rc;

	snprintf(path, sizeof(path), "%s/focal32.mdt", dir);
	if (read_whole(path, &mdt, &mdt_len)) {
		logf("open %s failed", path);
		return -1;
	}
	for (i = 0; i < 8; i++) {
		snprintf(path, sizeof(path), "%s/focal32.b%02d", dir, i);
		if (read_whole(path, &raw[i], &raw_len[i])) {
			logf("open %s failed", path);
			rc = -1;
			goto out;
		}
		segs[i].data = raw[i];
		segs[i].len = raw_len[i];
	}
	rc = fp5_mbn_from_mdt(mdt, mdt_len, segs, 8, &img, &img_len);
	if (rc) {
		logf("image place rc=%d", rc);
		goto out;
	}
	logf("image bytes=%zu mdt=%zu", img_len, mdt_len);
	memset(dist, 0, sizeof(dist));
	rc = fp5_tee_load_buffer(&s->tee, loader, img, img_len, "focal32", 7, dist,
				 sizeof(dist), app, &qret);
	logf("loadFromBuffer ioctl_rc=%d qret=%u %s dist='%s' app=%llu errno=%d",
	     rc ? -1 : 0, qret, verdict_name(qret), dist,
	     (unsigned long long)(rc ? 0 : *app), rc ? errno : 0);
	if (qret_out)
		*qret_out = qret;
	if (rc || qret)
		rc = qret == 16 ? 16 : -1;
	else
		rc = 0;
out:
	free(mdt);
	free(img);
	for (i = 0; i < 8; i++)
		free(raw[i]);
	return rc;
}

static void unload_stale(struct svc *s, uint64_t loader)
{
	struct tee_param_view p[3];
	uint8_t ob[4];
	uint32_t qret = 0;
	const char name[] = "focal32";

	memset(p, 0, sizeof(p));
	memset(ob, 0, sizeof(ob));
	p[0].attr = 8;
	p[0].a = (uint64_t)(uintptr_t)name;
	p[0].b = 7;
	p[1].attr = 9;
	p[1].a = (uint64_t)(uintptr_t)ob;
	p[1].b = sizeof(ob);
	p[2].attr = 12;
	if (fp5_tee_invoke(&s->tee, loader, 2, p, 3, &qret) || qret) {
		logf("lookupTA qret=%u errno=%d", qret, errno);
		return;
	}
	logf("lookupTA app=%llu", (unsigned long long)p[2].a);
	if (fp5_tee_invoke(&s->tee, p[2].a, 2, NULL, 0, &qret))
		logf("unload op2 errno=%d qret=%u", errno, qret);
	else
		logf("unload op2 qret=%u", qret);
	fp5_tee_invoke(&s->tee, p[2].a, 0xffff, NULL, 0, &qret);
}

static int open_and_load(struct svc *s, const char *dir)
{
	uint64_t env = 0, loader = 0;
	uint32_t qret = 0;
	int rc;

	if (fp5_tee_client_env(&s->tee, &env, &qret)) {
		logf("client_env errno=%d qret=%u", errno, qret);
		return -1;
	}
	logf("client_env qret=%u env=%llu", qret, (unsigned long long)env);
	if (reg_listener(s, env, LID_GP, 0x7e000))
		return -1;
	/*
	 * SCM REGISTER_LISTENER already owns id 0x2000 on a 64K tzmem
	 * buffer (mink then returns qret 12). Do not abort the load.
	 * Requests for that id are not delivered on this shm.
	 */
	if (reg_listener(s, env, LID_RPMB, 0x6400))
		logl("rpmb mink register failed; continue");
	if (reg_listener(s, env, LID_TIME, 0x5000) ||
	    reg_listener(s, env, LID_SSD, 0x1000))
		return -1;
	if (fp5_tee_open_service(&s->tee, env, 122, &loader, &qret)) {
		logf("open loader qret=%u errno=%d", qret, errno);
		return -1;
	}
	logf("open_loader qret=%u loader=%llu", qret, (unsigned long long)loader);
	rc = load_once(s, loader, dir, &s->app, &qret);
	if (rc == 16) {
		logl("deviation: lookupTA after ALREADY_LOADED");
		unload_stale(s, loader);
		rc = load_once(s, loader, dir, &s->app, &qret);
	}
	fp5_tee_invoke(&s->tee, loader, 0xffff, NULL, 0, &qret);
	fp5_tee_invoke(&s->tee, env, 0xffff, NULL, 0, &qret);
	return rc == 0 ? 0 : -1;
}

static int setup_ta(struct svc *s, int native)
{
	uint8_t ver[4] = { 2, 0, 0, 0 };
	uint8_t zero[4] = { 0, 0, 0, 0 };
	uint32_t spd = 8030000;
	char cfg[2048];
	int n;

	logf("buffers req=%u rsp=%u", REQ_SZ, RSP_SZ);
	if (send_cmd(s, fp5_op_version(), 4, ver, "VERSION"))
		return -1;
	if (send_cmd(s, fp5_op_set_km(), 0, NULL, "SET_KM"))
		return -1;
	n = fp5_sync_config(cfg, sizeof(cfg), native);
	if (n < 0 || send_cmd(s, fp5_op_sync(), (uint32_t)n + 1, cfg, "SYNC"))
		return -1;
	if (send_cmd(s, fp5_op_init_spi(), 0, NULL, "INIT_SPI"))
		return -1;
	if (send_cmd(s, fp5_op_set_spi(), 4, &spd, "SET_SPI"))
		return -1;
	if (send_cmd(s, fp5_op_probe(), 1, zero, "PROBE"))
		return -1;
	if (send_cmd(s, fp5_op_probe(), 1, zero, "PROBE2"))
		return -1;
	if (send_cmd(s, fp5_op_init_dev(), 0, NULL, "INIT_DEV"))
		return -1;
	note_serial(s);
	if (send_cmd(s, fp5_op_chip(), 0, NULL, "CHIP"))
		return -1;
	if (send_cmd(s, fp5_op_init(), 0, NULL, "INIT"))
		return -1;
	if (send_cmd(s, fp5_op_calib(), 0, NULL, "CALIB"))
		return -1;
	/*
	 * 0x100e copies 0x230 bytes into the stats object and publishes
	 * it. do_enroll writes a timestamp through that pointer. Until
	 * this runs the pointer is NULL and REPORT event 5 drops the app.
	 */
	{
		uint8_t stats[0x230];

		memset(stats, 0, sizeof(stats));
		if (send_cmd(s, fp5_op_stats(), 0x230, stats, "SYNC_STATS"))
			return -1;
		qsee_dump_delta("SYNC_STATS");
	}
	if (send_cmd(s, fp5_op_health(), 4, zero, "HEALTH"))
		return -1;
	return 0;
}

static void arm_report(struct svc *s, uint32_t ev)
{
	/* A leftover word at +0x2dc makes dispatch add a second length. */
	memset(s->req + FP5_PAY_OFF, 0, 0x2e0);
	fp5_event_fill(s->req + FP5_PAY_OFF, ev, 4, 3, 0);
	fp5_poison_rem(s->req + FP5_PAY_OFF);
}

static int lift_rearm(struct svc *s, unsigned *irq)
{
	uint32_t mode = fp5_wmode_up();

	if (send_cmd(s, fp5_op_wmode(), 4, &mode, "WMODE_UP"))
		return -1;
	wait_irq(irq, 1500);
	arm_report(s, 6);
	if (send_cmd(s, fp5_op_report(), 0x2e0, NULL, "REPORT_EV6"))
		return -1;
	mode = fp5_wmode_detect();
	return send_cmd(s, fp5_op_wmode(), 4, &mode, "WMODE_DETECT");
}

/* Trustlet stores "%s/ff_template_%d_%d.bin". With no base the file is
 * /ff_template_<gid>_<fid % 10>.bin. Unlock loads this empty path.
 * Enroll has to load it before the fid is chosen: the new id's last
 * digit is the first empty RAM slot, and an empty gallery is always
 * slot 0, which replaces ff_template_0_0.bin.
 * /data/vendor_de/0/fpdata looks up a different object and drops the RAM copy.
 */
static const char *group_path(void)
{
	return "";
}

static int set_group(struct svc *s, const char *path)
{
	uint8_t g[FP5_GROUP_CAP];
	int n = fp5_build_group(g, sizeof(g), path);

	if (n < 0) {
		logf("SET_GROUP path too long (%zu bytes, max %zu)",
		     path ? strlen(path) : (size_t)0, sizeof(g) - 5);
		return -1;
	}
	/* send_cmd fails on the invoke, not on the trustlet status word.
	 * SET_GROUP rc -2 (no share template) still returns 0 here. */
	if (send_cmd(s, fp5_op_set_group(), (uint32_t)n, g, "SET_GROUP"))
		return -1;
	return send_cmd(s, fp5_op_enum(), 0, NULL, "ENUM");
}

static int open_enroll(struct svc *s)
{
	uint8_t hat[0x4a];
	uint64_t challenge = 0;
	uint32_t fid = 0;
	int nonzero = 0;
	int i;
	char cfg[2048];
	int n;

	n = fp5_sync_config(cfg, sizeof(cfg), 1);
	if (n > 0)
		send_cmd(s, fp5_op_sync(), (uint32_t)n + 1, cfg, "SYNC_ENROLL");
	logf("PRE_ENROLL opcode=0x%x", fp5_op_pre_enroll());
	if (send_cmd(s, fp5_op_pre_enroll(), 0, NULL, "PRE_ENROLL"))
		return -1;
	memcpy(&challenge, s->req + FP5_PAY_OFF, 8);
	for (i = 0; i < 8; i++)
		nonzero += ((uint8_t *)&challenge)[i] != 0;
	logf("PRE_ENROLL rc=%d challenge_bytes=%d", (int)s->last_rc, nonzero);
	fp5_hat_enroll(hat, challenge);
	logf("ENROLL opcode=0x%x", fp5_op_enroll());
	if (send_cmd(s, fp5_op_enroll(), 0x4a, hat, "ENROLL"))
		return -1;
	memcpy(&fid, s->req + FP5_PAY_OFF, 4);
	logf("ENROLL fid=%u rc=%d text rsp=%d req=%d", fid, (int)s->last_rc,
	     buf_has(s->rsp_out, RSP_SZ, "focaltech"),
	     buf_has(s->req, REQ_SZ, "focaltech"));
	return 0;
}

/* One capture after enroll, not scored and not reported. */
static int probe_capture(struct svc *s)
{
	uint32_t mode = fp5_wmode_detect();

	struct timespec t0, t1;
	long ms;

	if (s->last_rc != 0)
		return 2;
	if (send_cmd(s, fp5_op_wmode(), 4, &mode, "WMODE_DETECT"))
		return -1;
	{
		uint8_t z[4] = { 0 };

		if (send_cmd(s, fp5_op_query(), 4, z, "QEV_PROBE"))
			return -1;
		logf("QEV_PROBE itype=0x%x esd=%d saw=%d", s->last.itype,
		     s->last.esd, s->last.saw_itype);
	}
	fp5_capture_fill(s->req + FP5_PAY_OFF, 3, 0, fp5_hal_flag_enroll());
	clock_gettime(CLOCK_MONOTONIC, &t0);
	if (send_cmd(s, fp5_op_capture(), 0x24, NULL, "CAP_PROBE"))
		return -1;
	clock_gettime(CLOCK_MONOTONIC, &t1);
	ms = (t1.tv_sec - t0.tv_sec) * 1000 + (t1.tv_nsec - t0.tv_nsec) / 1000000;
	logf("CAP_PROBE ms=%ld", ms);
	dump_runs("CAP rsp", s->rsp_out, RSP_SZ);
	dump_runs("CAP req", s->req, REQ_SZ);
	{
		uint32_t w[16];
		int i;

		for (i = 0; i < 16; i++)
			memcpy(&w[i], s->req + FP5_PAY_OFF + (size_t)i * 4, 4);
		logf("CAP_PROBE pay %08x %08x %08x %08x %08x %08x %08x %08x",
		     w[0], w[1], w[2], w[3], w[4], w[5], w[6], w[7]);
		logf("CAP_PROBE pay2 %08x %08x %08x %08x %08x %08x %08x %08x",
		     w[8], w[9], w[10], w[11], w[12], w[13], w[14], w[15]);
	}
	logf("CAP_PROBE needles irq=%d frame=%d err=%d fid=%d avgv=%d",
	     buf_has(s->rsp_out, RSP_SZ, "interrupt") || buf_has(s->req, REQ_SZ, "interrupt"),
	     buf_has(s->rsp_out, RSP_SZ, "frame") || buf_has(s->req, REQ_SZ, "frame"),
	     buf_has(s->rsp_out, RSP_SZ, "error at") || buf_has(s->req, REQ_SZ, "error at"),
	     buf_has(s->rsp_out, RSP_SZ, "fid") || buf_has(s->req, REQ_SZ, "fid"),
	     buf_has(s->rsp_out, RSP_SZ, "avgv") || buf_has(s->req, REQ_SZ, "avgv"));
	return s->last_rc == 0 ? 0 : 2;
}

/* SET_GROUP on the unlock path, then the bytes from that command only.
 * The full qsee ring still holds older "there is 1 template loaded"
 * lines, which must not count. ENUM is sent only after a real load:
 * it returns -203 and does not remove a template.
 */
static int gallery_in_cmd(struct svc *s, int *count)
{
	if (qsee_delta && qsee_delta_len &&
	    fp5_gallery_loaded(qsee_delta, qsee_delta_len, count))
		return 1;
	if (s->rsp_out && fp5_gallery_loaded(s->rsp_out, RSP_SZ, count))
		return 1;
	if (s->req && fp5_gallery_loaded(s->req, REQ_SZ, count))
		return 1;
	return 0;
}

static int enroll_load_gallery(struct svc *s)
{
	uint8_t g[FP5_GROUP_CAP];
	int n = fp5_build_group(g, sizeof(g), group_path());
	int count = -1;

	if (n < 0)
		return -1;
	if (send_cmd(s, fp5_op_set_group(), (uint32_t)n, g, "SET_GROUP"))
		return -1;
	if (!gallery_in_cmd(s, &count)) {
		logl("enroll refuse: trustlet did not report a template count");
		return 2;
	}
	if (count < 1) {
		logf("enroll refuse: loaded=%d; not minting a finger", count);
		return 2;
	}
	logf("enroll gallery loaded=%d", count);
	if (send_cmd(s, fp5_op_enum(), 0, NULL, "ENUM"))
		return -1;
	return 0;
}

static int enroll_finger(struct svc *s)
{
	unsigned irq = read_irq();
	uint32_t rem = 0xffffffffu;
	int sample;
	int wrote = 0;
	long save_ms = 0;
	int loaded;

	/* Fill RAM before PRE_ENROLL. The trustlet then gives the new
	 * finger the next free slot instead of replacing slot 0. A load
	 * that does not see the current finger stops here, before any
	 * fid is generated and before SAVE.
	 */
	loaded = enroll_load_gallery(s);
	if (loaded)
		return loaded;
	if (open_enroll(s))
		return -1;
	if (s->last_rc != 0) {
		logf("ENROLL rejected rc=%d", (int)s->last_rc);
		return 2;
	}
	for (sample = 0; sample < 24 && rem != 0; sample++) {
		uint32_t mode = fp5_wmode_detect();
		uint32_t av[3];
		int fi;
		int wait_ms = sample == 0 ? 90000 : 45000;

		if (send_cmd(s, fp5_op_wmode(), 4, &mode, "WMODE_DETECT"))
			return -1;
		logf("wait finger sample=%d irq=%u", sample, irq);
		if (!wait_irq(&irq, wait_ms)) {
			logf("no finger irq sample=%d", sample);
			break;
		}
		{
			uint8_t z[4] = { 0 };

			if (send_cmd(s, fp5_op_query(), 4, z, "QEV"))
				return -1;
		}
		if (!fp5_real_down(s->last.itype, s->last.esd)) {
			logf("skip leftover itype=0x%x esd=%d", s->last.itype, s->last.esd);
			continue;
		}
		logf("REAL DOWN itype=0x%x", s->last.itype);
		for (fi = 0; fi < 3; fi++) {
			fp5_capture_fill(s->req + FP5_PAY_OFF, 3, (uint32_t)fi,
					 fp5_hal_flag_enroll());
			if (send_cmd(s, fp5_op_capture(), 0x24, NULL, "CAP"))
				return -1;
			av[fi] = s->last.avgv;
			if ((uint32_t)s->last_rc == 0xffffff37u) {
				logl("CAP null — skip REPORT");
				av[fi] = 0;
				break;
			}
		}
		if (!fp5_burst_full(av, 3)) {
			logf("PRESS REJECT avgv=%u,%u,%u", av[0], av[1], av[2]);
			if (lift_rearm(s, &irq))
				return -1;
			continue;
		}
		arm_report(s, 5);
		if (send_cmd(s, fp5_op_report(), 0x2e0, NULL, "REPORT_EV5"))
			return -1;
		rem = fp5_req_rem(s->req);
		if (rem == 0xa5a5a5a5u || rem > 20) {
			logf("rem unread 0x%x", rem);
			rem = 0xffffffffu;
		} else {
			logf("rem=%u sample=%d", rem, sample);
		}
		if (rem == 0) {
			uint32_t flag = fp5_save_flag();
			struct timespec a, b;
			uint32_t cmd;
			uint16_t result;
			int valid, tmpl;

			memset(s->req + FP5_PAY_OFF, 0, 0x230);
			memcpy(s->req + FP5_PAY_OFF, &flag, 4);
			pthread_mutex_lock(&s->mu);
			s->last_rpmb_cmd = 0;
			s->last_rpmb_valid = 0;
			s->template_write = 0;
			s->persist_write = 0;
			pthread_mutex_unlock(&s->mu);
			clock_gettime(CLOCK_MONOTONIC, &a);
			logf("SAVE opcode=0x%x flag=0x%x", fp5_op_save(), flag);
			if (send_cmd(s, fp5_op_save(), 4, NULL, "SAVE"))
				return -1;
			clock_gettime(CLOCK_MONOTONIC, &b);
			save_ms = (b.tv_sec - a.tv_sec) * 1000 +
				  (b.tv_nsec - a.tv_nsec) / 1000000;
			pthread_mutex_lock(&s->mu);
			cmd = s->last_rpmb_cmd;
			result = s->last_rpmb_result;
			valid = s->last_rpmb_valid;
			tmpl = s->template_write;
			pthread_mutex_unlock(&s->mu);
			wrote = (valid && cmd == 0x103 && result == 0) || tmpl;
			logf("SAVE ms=%ld rpmb_cmd=0x%x rpmb_result=0x%x valid=%d template=%d name=%s",
			     save_ms, cmd, result, valid, tmpl, s->template_name);
			if (set_group(s, group_path()))
				return -1;
		}
		if (lift_rearm(s, &irq))
			return -1;
	}
	logf("enroll done rem=%u wrote=%d save_ms=%ld", rem, wrote, save_ms);
	if (rem == 0 && wrote && save_ms >= 30)
		return 0;
	return 2;
}

/*
 * 1 while the finger from this press is still down, 0 once it has lifted,
 * -1 if the interrupt query failed. An unchanged irq_count needs no query.
 * A new edge is classified with QEV and is not passed to fp5_att_irq, so a
 * lift cannot look like another finger-down of this attempt.
 */
static int finger_held(struct svc *s, unsigned *irq, int already)
{
	unsigned now = read_irq();
	unsigned base = *irq;
	int held;
	uint8_t z[4] = { 0 };

	if (now == base)
		return 1;
	if (send_cmd(s, fp5_op_query(), 4, z, "QEV"))
		return -1;
	held = fp5_held_for_retry(already, base, now, s->last.saw_itype,
				  s->last.itype, s->last.esd);
	if (held && !fp5_finger_held(base, now, s->last.saw_itype,
				     s->last.itype, s->last.esd))
		logf("still down after idle query itype=0x%x", s->last.itype);
	if (s->last.saw_itype)
		*irq = now;
	return held;
}

/*
 * Up to three 3-frame bursts while the finger stays down. The first hit
 * returns at once. Scored misses share one AUTH FAIL, so the watcher adds
 * one strike. Bursts with no in-range frame are not reported; if every
 * burst was like that, one AUTH empty is printed and it is not a strike.
 * Cancel before any scored report returns cancelled and prints no FAIL.
 */
static int auth_press(struct svc *s, int which, unsigned *irq,
		      uint32_t itype, int esd, int already)
{
	int burst, scored = 0, saw_empty = 0, cut = 0;
	uint32_t empty_av[3] = { 0, 0, 0 };
	uint32_t miss_avgv = 0, miss_fid = 0;
	int miss_rc = 0;

	for (burst = 0; burst < 3 && !cut; burst++) {
		uint32_t av[3] = { 0, 0, 0 };
		uint32_t fid = 0;
		int fi, matched, verdict, held;

		if (burst > 0) {
			if (serve_on && serve_poll_cmds()) {
				if (!scored) {
					logf("AUTH %d cancelled", which);
					return FP5_ATT_CANCELLED;
				}
				break;
			}
			held = finger_held(s, irq, already);
			if (held < 0)
				return -1;
			if (!fp5_retry_more(burst, held))
				break;
		}
		for (fi = 0; fi < 3; fi++) {
			fp5_capture_fill(s->req + FP5_PAY_OFF, 3, (uint32_t)fi,
					 fp5_hal_flag_enroll());
			if (send_cmd(s, fp5_op_capture(), 0x24, NULL, "CAP"))
				return -1;
			av[fi] = s->last.avgv;
			if (serve_on && serve_poll_cmds()) {
				if (!scored) {
					logf("AUTH %d cancelled during capture", which);
					return FP5_ATT_CANCELLED;
				}
				cut = 1;
				break;
			}
		}
		if (cut)
			break;
		if (!fp5_burst_ok(av, 3)) {
			saw_empty = 1;
			empty_av[0] = av[0];
			empty_av[1] = av[1];
			empty_av[2] = av[2];
			continue;
		}
		/*
		 * All three captures are already in the trustlet. Report the
		 * burst as a whole, including a frame with avgv >= 600.
		 */
		logf("AUTH %d burst avgv=%u,%u,%u", which, av[0], av[1], av[2]);
		arm_report(s, 5);
		if (send_cmd(s, fp5_op_report(), 0x2e0, NULL, "REPORT_EV5"))
			return -1;
		qsee_dump_delta("REPORT_EV5");
		memcpy(&fid, s->req + FP5_PAY_OFF + 0x10, 4);
		logf("AUTH %d report rc=%d fid=%u", which, (int)s->last_rc, fid);
		if (serve_on)
			serve_poll_cmds();
		matched = fp5_auth_burst(itype, esd, av, 3, s->last_rc, fid);
		verdict = matched ? FP5_ATT_HIT : FP5_ATT_MISS;
		if (serve_on)
			verdict = fp5_att_finish(&serve_att, matched);
		if (verdict == FP5_ATT_HIT) {
			logf("AUTH HIT %d itype=0x%x avgv=%u fid=%u rc=%d", which,
			     itype, fp5_burst_pick(av, 3), fid, (int)s->last_rc);
			return 0;
		}
		if (verdict == FP5_ATT_CANCELLED) {
			/* Matched, but the watcher cancelled. Never say HIT. */
			logf("AUTH %d cancelled after match", which);
			return FP5_ATT_CANCELLED;
		}
		scored++;
		miss_avgv = fp5_burst_pick(av, 3);
		miss_fid = fid;
		miss_rc = (int)s->last_rc;
	}
	if (fp5_press_strikes(scored)) {
		logf("AUTH FAIL %d itype=0x%x esd=%d avgv=%u fid=%u rc=%d",
		     which, itype, esd, miss_avgv, miss_fid, miss_rc);
		return 2;
	}
	if (saw_empty) {
		logf("AUTH %d empty avgv=%u,%u,%u", which, empty_av[0],
		     empty_av[1], empty_av[2]);
		return 2;
	}
	return 2;
}

/* DSI dpms "On". Missing sysfs is off. The path is cached. */
static int dsi_is_on(void)
{
	static char path[256];
	char buf[8];
	int fd, n;

	if (!path[0]) {
		DIR *dir = opendir("/sys/class/drm");
		struct dirent *ent;

		if (!dir)
			return 0;
		while ((ent = readdir(dir)) != NULL) {
			if (strncmp(ent->d_name, "card", 4) != 0)
				continue;
			if (strstr(ent->d_name, "-DSI-") == NULL)
				continue;
			n = snprintf(path, sizeof(path),
				     "/sys/class/drm/%s/dpms", ent->d_name);
			if (n < 0 || (size_t)n >= sizeof(path))
				path[0] = 0;
			break;
		}
		closedir(dir);
		if (!path[0])
			return 0;
	}
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return 0;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n < 2)
		return 0;
	buf[n] = 0;
	return (buf[0] == 'O' || buf[0] == 'o') &&
	       (buf[1] == 'n' || buf[1] == 'N');
}

/* 1 when a query says a finger is down, 0 when it does not, -1 on invoke. */
static int query_held(struct svc *s, uint32_t *itype, int *esd)
{
	uint8_t z[4] = { 0 };

	if (send_cmd(s, fp5_op_query(), 4, z, "QEV"))
		return -1;
	if (!fp5_down_at_arm(s->last.saw_itype, s->last.itype, s->last.esd))
		return 0;
	*itype = s->last.itype;
	*esd = s->last.esd;
	return 1;
}

static int auth_once(struct svc *s, int which)
{
	unsigned irq = read_irq();
	/*
	 * Mode 1 is chip wait-touch. The trustlet names that FDT_DOWN_DETECT.
	 * Mode 9 only pokes an SPI sequence and returns 0, so a fresh Match
	 * never sees the pin move. Enroll still uses mode 9 because the
	 * enroll command already has the chip scanning.
	 */
	uint32_t mode = fp5_wmode_touch();
	uint32_t itype = 0;
	int esd = 0, down = 0, from_query = 0;
	int left = 90000;
	uint8_t hat[0x4a];
	uint8_t z[4] = { 0 };

	/*
	 * 0x2008 stores operation mode 2 (do_authenticate) and returns 0
	 * when that store works. The finger is compared later, inside the
	 * image report. A 0 from this command is not a match.
	 */
	fp5_hat_auth(hat);
	if (send_cmd(s, fp5_op_auth(), fp5_auth_plen(), hat, "AUTH_ARM"))
		return -1;
	if (s->last_rc != 0) {
		logf("AUTH FAIL %d itype=0x0 esd=0 avgv=0 rc=%d fid=0", which,
		     (int)s->last_rc);
		return 2;
	}
	if (serve_on) {
		/*
		 * Fresh attempt: the baseline is this arm's irq_count. An irq
		 * change while cancelled is ignored. A finger already down
		 * is the query below, and cancel still refuses the capture.
		 * Commands that came in with "auth" are applied now.
		 */
		fp5_att_begin(&serve_att, irq);
		if (serve_poll_cmds()) {
			logf("AUTH %d cancelled", which);
			return FP5_ATT_CANCELLED;
		}
	}
	logf("AUTH %d arm mode=%u irq=%u", which, mode, irq);
	if (send_cmd(s, fp5_op_wmode(), 4, &mode, "WMODE_TOUCH"))
		return -1;
	if (serve_on && serve_poll_cmds()) {
		logf("AUTH %d cancelled", which);
		return FP5_ATT_CANCELLED;
	}
	/*
	 * irq was sampled before AUTH_ARM. A finger already down sits
	 * inside that baseline, and wait_irq will not see it. Ask once.
	 * A mode-switch leftover is not a finger.
	 */
	{
		int held = query_held(s, &itype, &esd);

		if (held < 0)
			return -1;
		if (held) {
			down = 1;
			from_query = 1;
			logf("AUTH %d already down itype=0x%x", which, itype);
		} else if (s->last.saw_itype) {
			logf("skip leftover itype=0x%x esd=%d",
			     s->last.itype, s->last.esd);
		}
	}
	{
		int panel = dsi_is_on();

		while (!down && left > 0) {
			int got = wait_slice(&irq, &left, 200);
			int now_panel;

			if (serve_on && serve_att.cancelled) {
				logf("AUTH %d cancelled", which);
				return FP5_ATT_CANCELLED;
			}
			if (got) {
				if (send_cmd(s, fp5_op_query(), 4, z, "QEV"))
					return -1;
				itype = s->last.itype;
				esd = s->last.esd;
				if (fp5_real_down(itype, esd)) {
					down = 1;
					break;
				}
				/* Keep the screen on PRESS. An AUTH-prefixed skip would say LIFT. */
				logf("skip leftover itype=0x%x esd=%d", itype, esd);
				continue;
			}
			/*
			 * The power key turned the panel on. The GPIO edge
			 * can be missing when the CPU was suspended. Ask
			 * once on that rise.
			 */
			now_panel = dsi_is_on();
			if (fp5_query_on_panel_rise(panel, now_panel)) {
				int held = query_held(s, &itype, &esd);

				if (held < 0)
					return -1;
				if (held) {
					down = 1;
					from_query = 1;
					logf("AUTH %d held at panel itype=0x%x",
					     which, itype);
				}
			}
			panel = now_panel;
		}
	}
	if (!down) {
		if (serve_on && serve_att.cancelled) {
			logf("AUTH %d cancelled", which);
			return FP5_ATT_CANCELLED;
		}
		logf("AUTH %d no finger", which);
		return FP5_ATT_NONE;
	}
	/* The QEV round trip takes time; a cancel may have come in. */
	if (serve_on && (serve_poll_cmds() || !fp5_att_down(&serve_att))) {
		logf("AUTH %d cancelled after down", which);
		return FP5_ATT_CANCELLED;
	}
	logf("AUTH %d REAL DOWN itype=0x%x", which, itype);
	return auth_press(s, which, &irq, itype, esd, from_query);
}

/*
 * Stay loaded and wait for commands, so a match does not pay for the
 * trustlet load and setup every time. Idle here is a blocking read on
 * stdin: no polling, no sensor commands.
 *
 *   in:  auth | cancel | quit   (EOF is quit)
 *   out: "SERVE ready" once, "SERVE idle" before each wait for a
 *        command, "SERVE result <auth_once rc>" after each auth. A
 *        cancelled auth prints "AUTH <n> cancelled..." and result 3,
 *        also when the finger was already down or had matched. An
 *        auth sent while one runs gets "SERVE reject auth busy"; an
 *        unknown line gets "SERVE reject unknown". cancel while idle
 *        does nothing.
 */
static int serve_loop(struct svc *s)
{
	int n = 0, rc = 0;

	irq_fd = open(IRQ_COUNT, O_RDONLY | O_CLOEXEC);
	serve_on = 1;
	if (set_group(s, group_path())) {
		rc = 1;
		goto out;
	}
	logl("SERVE ready");
	while (!serve_quit) {
		int cmd, a;

		logl("SERVE idle");
		for (;;) {
			cmd = serve_read_cmd(-1);
			if (cmd == FP5_CMD_AUTH || cmd == FP5_CMD_QUIT)
				break;
			if (cmd == FP5_CMD_UNKNOWN)
				logl("SERVE reject unknown");
		}
		if (cmd == FP5_CMD_QUIT)
			break;
		a = auth_once(s, ++n);
		logf("SERVE result %d", a);
		if (a < 0) {
			rc = 1;
			break;
		}
	}
out:
	serve_on = 0;
	if (irq_fd >= 0)
		close(irq_fd);
	irq_fd = -1;
	return rc;
}

static void free_bufs(struct svc *s)
{
	free(s->req);
	free(s->rsp);
	free(s->req_out);
	free(s->rsp_out);
	s->req = s->rsp = s->req_out = s->rsp_out = NULL;
	if (s->bsg >= 0)
		close(s->bsg);
	s->bsg = -1;
}

static int alloc_bufs(struct svc *s)
{
	s->req = calloc(1, REQ_SZ);
	s->rsp = calloc(1, RSP_SZ);
	s->req_out = calloc(1, REQ_SZ);
	s->rsp_out = calloc(1, RSP_SZ);
	if (!s->req || !s->rsp || !s->req_out || !s->rsp_out)
		return -1;
	s->bsg = open("/dev/bsg/0:0:0:49476", O_RDWR);
	if (s->bsg < 0)
		logf("bsg open: %s", strerror(errno));
	return 0;
}

int main(int argc, char **argv)
{
	const char *mode = argc > 1 ? argv[1] : "enroll";
	const char *dir = argc > 2 ? argv[2] : "/lib/firmware/qsee";
	struct svc s;
	int rc, i;

	setvbuf(stdout, NULL, _IOLBF, 0);
	{
		struct sigaction sa;

		memset(&sa, 0, sizeof(sa));
		sa.sa_handler = sigusr1_nop;
		sigaction(SIGUSR1, &sa, NULL);
	}
	memset(&s, 0, sizeof(s));
	s.tee.fd = -1;
	s.bsg = -1;
	pthread_mutex_init(&s.mu, NULL);
	if (mount_persist() || sensor_on())
		return 1;
	if (fp5_tee_open(&s.tee, "/dev/tee0")) {
		logf("open /dev/tee0: %s", strerror(errno));
		return 1;
	}
	if (alloc_bufs(&s)) {
		logl("out of memory for session buffers");
		free_bufs(&s);
		fp5_tee_close(&s.tee);
		return 1;
	}
	if (pthread_create(&s.thr, NULL, supp_main, &s)) {
		logl("supp thread failed");
		free_bufs(&s);
		fp5_tee_close(&s.tee);
		return 1;
	}
	/* The watch thread reads /dev/qsee_log every 30 ms for a debug
	 * file. A serve session can idle for hours, so it goes without. */
	if (strcmp(mode, "serve"))
		watch_on = pthread_create(&watch_thr, NULL, qsee_watch_main, NULL) == 0;
	while (!s.ready)
		usleep(1000);
	usleep(20 * 1000);
	if (open_and_load(&s, dir)) {
		rc = 1;
		goto done;
	}
	if (setup_ta(&s, 1)) {
		rc = 1;
		goto done;
	}
	if (!strcmp(mode, "init")) {
		rc = 0;
	} else if (!strcmp(mode, "reopen")) {
		/* setup_ta already read the segment into a new TA.
		 * argv[3] is the group path. Empty matches a save that
		 * stored /ff_template_0_0.bin via "%s/%s" with no base.
		 */
		const char *path = argc > 3 ? argv[3] : group_path();

		logf("reopen path '%s'", path);
		rc = set_group(&s, path) ? 1 : 0;
	} else if (!strcmp(mode, "begin")) {
		rc = open_enroll(&s);
		if (rc == 0 && s.last_rc != 0)
			rc = 2;
	} else if (!strcmp(mode, "probe") || !strcmp(mode, "probe-mem") ||
		   !strcmp(mode, "probe-obj") || !strcmp(mode, "probe-off")) {
		if (!strcmp(mode, "probe-mem"))
			s.cap_try = 1;
		else if (!strcmp(mode, "probe-obj"))
			s.cap_try = 2;
		else if (!strcmp(mode, "probe-off"))
			s.cap_try = 3;
		rc = open_enroll(&s);
		if (rc == 0)
			rc = probe_capture(&s);
	} else if (!strcmp(mode, "report-empty")) {
		/* No finger. One REPORT after enroll-open. Optional plen
		 * in argv[3] (default 0x2e0) to see which size kills the app.
		 */
		uint32_t plen = 0x2e0;

		if (argc > 3)
			plen = (uint32_t)strtoul(argv[3], NULL, 0);
		rc = open_enroll(&s);
		if (rc == 0 && s.last_rc != 0)
			rc = 2;
		if (rc == 0) {
			uint32_t w4 = 0, wc = 0, w30 = 0, w34 = 0;

			arm_report(&s, 5);
			memcpy(&w4, s.req + FP5_PAY_OFF + 4, 4);
			memcpy(&wc, s.req + FP5_PAY_OFF + 0xc, 4);
			memcpy(&w30, s.req + FP5_PAY_OFF + 0x30, 4);
			memcpy(&w34, s.req + FP5_PAY_OFF + 0x34, 4);
			logf("REPORT_EMPTY plen=0x%x pay+4=0x%x +0xc=0x%x +0x30=0x%x +0x34=0x%x",
			     plen, w4, wc, w30, w34);
			rc = send_cmd(&s, fp5_op_report(), plen, NULL, "REPORT_EMPTY");
			if (rc)
				rc = 2;
		}
	} else if (!strcmp(mode, "enroll")) {
		rc = enroll_finger(&s);
	} else if (!strcmp(mode, "arm1")) {
		/*
		 * No finger and no capture. Match was arming mode 9, which
		 * returns 0 after an SPI poke and leaves the pin quiet.
		 * Mode 1 is the chip wait-touch the Android event thread sends
		 * (the trustlet then logs FDT_DOWN_DETECT). Watch the GPIO
		 * for a short window, query once, and leave.
		 */
		unsigned irq0 = read_irq();
		unsigned irq = irq0;
		unsigned edges = 0;
		uint32_t wmode = fp5_wmode_touch();
		int ms;
		uint8_t z[4] = { 0 };

		if (set_group(&s, group_path())) {
			rc = 1;
			goto done;
		}
		logf("ARM1 before irq=%u mode=%u", irq0, wmode);
		if (send_cmd(&s, fp5_op_wmode(), 4, &wmode, "WMODE_TOUCH")) {
			rc = 1;
		} else {
			for (ms = 0; ms < 2500; ms += 20) {
				unsigned now = read_irq();

				if (now != irq) {
					edges++;
					logf("ARM1 edge irq=%u was=%u", now, irq);
					irq = now;
				}
				usleep(20 * 1000);
			}
			logf("ARM1 watch irq=%u delta=%u edges=%u", irq,
			     irq - irq0, edges);
			if (send_cmd(&s, fp5_op_query(), 4, z, "QEV_ARM1"))
				rc = 1;
			else {
				logf("ARM1 alive itype=0x%x esd=%d rc=%d edges=%u",
				     s.last.itype, s.last.esd, (int)s.last_rc,
				     edges);
				rc = 0;
			}
		}
	} else if (!strcmp(mode, "autharm")) {
		uint8_t hat[0x4a];

		fp5_hat_auth(hat);
		if (set_group(&s, group_path())) {
			rc = 1;
			goto done;
		}
		logf("AUTHARM plen=0x%x", fp5_auth_plen());
		if (send_cmd(&s, fp5_op_auth(), fp5_auth_plen(), hat, "AUTH_ARM"))
			rc = 1;
		else {
			logf("AUTHARM rc=%d", (int)s.last_rc);
			rc = s.last_rc == 0 ? 0 : 2;
		}
	} else if (!strcmp(mode, "auth")) {
		int a, b;

		if (set_group(&s, group_path())) {
			rc = 1;
			goto done;
		}
		a = auth_once(&s, 1);
		b = auth_once(&s, 2);
		logf("auth pair %d %d", a, b);
		rc = (a == 0 && b == 0) ? 0 : 2;
	} else if (!strcmp(mode, "unlock")) {
		/* One press for the Phosh lock screen. Match still asks twice.
		 * Setup and calibration run once. A window with no finger
		 * arms again in this process, so the next press is not a
		 * fresh reset with the finger already down. A hit or a
		 * scored miss ends it; the watcher starts the next one.
		 */
		int a, n = 0;

		if (set_group(&s, group_path())) {
			rc = 1;
			goto done;
		}
		do {
			a = auth_once(&s, ++n);
		} while (a == FP5_ATT_NONE);
		logf("unlock once %d", a);
		rc = a == 0 ? 0 : (a < 0 ? 1 : 2);
	} else if (!strcmp(mode, "serve")) {
		rc = serve_loop(&s);
	} else {
		logf("unknown mode %s", mode);
		rc = 1;
	}
done:
	s.stop = 1;
	watch_stop = 1;
	fp5_tee_close(&s.tee);
	pthread_kill(s.thr, SIGUSR1);
	pthread_join(s.thr, NULL);
	if (watch_on)
		pthread_join(watch_thr, NULL);
	for (i = 0; i < s.nls; i++)
		fp5_tee_shm_free(&s.ls[i].shm);
	free(qsee_buf);
	free(qsee_delta);
	qsee_buf = NULL;
	qsee_delta = NULL;
	free_bufs(&s);
	logf("session_exit:%d", rc);
	return rc == 0 ? 0 : 1;
}
