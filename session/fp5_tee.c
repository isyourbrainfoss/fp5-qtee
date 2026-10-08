#include "fp5_tee.h"

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#define TEE_IOC_MAGIC 0xa4
#define TEE_IOC_BASE 0

struct tee_ioctl_buf_data {
	uint64_t buf_ptr;
	uint64_t buf_len;
};

#define TEE_IOC_OBJECT_INVOKE \
	_IOR(TEE_IOC_MAGIC, TEE_IOC_BASE + 10, struct tee_ioctl_buf_data)

#define TEE_UBUF_IN 8
#define TEE_UBUF_OUT 9
#define TEE_OBJ_OUT 12

struct tee_ioctl_object_invoke_arg {
	uint64_t id;
	uint32_t op;
	uint32_t ret;
	uint32_t num_params;
	uint32_t pad;
	struct tee_param_view params[];
};

int fp5_tee_open(struct fp5_tee *t, const char *dev)
{
	t->fd = open(dev ? dev : "/dev/tee0", O_RDWR);
	return t->fd < 0 ? -1 : 0;
}

void fp5_tee_close(struct fp5_tee *t)
{
	if (t->fd >= 0)
		close(t->fd);
	t->fd = -1;
}

int fp5_tee_invoke(struct fp5_tee *t, uint64_t id, uint32_t op,
		   struct tee_param_view *params, uint32_t n, uint32_t *qret)
{
	size_t len = sizeof(struct tee_ioctl_object_invoke_arg) +
		     n * sizeof(struct tee_param_view);
	struct tee_ioctl_object_invoke_arg *arg;
	struct tee_ioctl_buf_data buf;
	int rc;

	arg = calloc(1, len);
	if (!arg)
		return -1;
	arg->id = id;
	arg->op = op;
	arg->num_params = n;
	if (n)
		memcpy(arg->params, params, n * sizeof(*params));
	buf.buf_ptr = (uint64_t)(uintptr_t)arg;
	buf.buf_len = len;
	rc = ioctl(t->fd, TEE_IOC_OBJECT_INVOKE, &buf);
	if (qret)
		*qret = arg->ret;
	if (n)
		memcpy(params, arg->params, n * sizeof(*params));
	free(arg);
	return rc;
}

int fp5_tee_client_env(struct fp5_tee *t, uint64_t *env, uint32_t *qret)
{
	static const uint8_t cred[] = {
		0xa2, 0x01, 0x00, 0x06, 0x1b,
		0, 0, 0, 0, 0, 0, 0, 0
	};
	struct tee_param_view p[2];
	uint32_t qr = 0;
	int rc;

	memset(p, 0, sizeof(p));
	p[0].attr = TEE_UBUF_IN;
	p[0].a = (uint64_t)(uintptr_t)cred;
	p[0].b = sizeof(cred);
	p[1].attr = TEE_OBJ_OUT;
	rc = fp5_tee_invoke(t, FP5_TEE_OBJ_NULL, 1, p, 2, &qr);
	if (qret)
		*qret = qr;
	if (rc || qr)
		return -1;
	*env = p[1].a;
	return 0;
}

int fp5_tee_open_service(struct fp5_tee *t, uint64_t env, uint32_t uid,
			 uint64_t *svc, uint32_t *qret)
{
	struct tee_param_view p[2];
	uint32_t qr = 0;
	int rc;

	memset(p, 0, sizeof(p));
	p[0].attr = TEE_UBUF_IN;
	p[0].a = (uint64_t)(uintptr_t)&uid;
	p[0].b = sizeof(uid);
	p[1].attr = TEE_OBJ_OUT;
	rc = fp5_tee_invoke(t, env, 0, p, 2, &qr);
	if (qret)
		*qret = qr;
	if (rc || qr)
		return -1;
	*svc = p[1].a;
	return 0;
}

int fp5_tee_load_buffer(struct fp5_tee *t, uint64_t loader,
			const void *image, size_t image_len,
			const char *name, size_t name_len,
			char *dist, size_t dist_cap,
			uint64_t *app, uint32_t *qret)
{
	struct tee_param_view p[4];
	uint32_t qr = 0;
	int rc;

	if (dist_cap)
		memset(dist, 0, dist_cap);
	memset(p, 0, sizeof(p));
	p[0].attr = TEE_UBUF_IN;
	p[0].a = (uint64_t)(uintptr_t)image;
	p[0].b = image_len;
	p[1].attr = TEE_UBUF_IN;
	p[1].a = (uint64_t)(uintptr_t)name;
	p[1].b = name_len;
	p[2].attr = TEE_UBUF_OUT;
	p[2].a = (uint64_t)(uintptr_t)dist;
	p[2].b = dist_cap;
	p[3].attr = TEE_OBJ_OUT;
	rc = fp5_tee_invoke(t, loader, 1, p, 4, &qr);
	if (qret)
		*qret = qr;
	if (rc || qr)
		return -1;
	*app = p[3].a;
	return 0;
}

struct tee_ioctl_shm_alloc_data {
	uint64_t size;
	uint32_t flags;
	int32_t id;
};

#define TEE_IOC_SHM_ALLOC \
	_IOWR(TEE_IOC_MAGIC, TEE_IOC_BASE + 1, struct tee_ioctl_shm_alloc_data)

#define TEE_IOC_SUPPL_RECV \
	_IOR(TEE_IOC_MAGIC, TEE_IOC_BASE + 6, struct tee_ioctl_buf_data)
#define TEE_IOC_SUPPL_SEND \
	_IOR(TEE_IOC_MAGIC, TEE_IOC_BASE + 7, struct tee_ioctl_buf_data)

#define FP5_VAL_INOUT 3u
#define FP5_VAL_OUT 2u
#define FP5_META 0x100u

struct fp5_supp_recv_ioctl {
	uint32_t func;
	uint32_t num_params;
	struct tee_param_view params[8];
};

struct fp5_supp_send_ioctl {
	uint32_t ret;
	uint32_t num_params;
	struct tee_param_view params[8];
};

int fp5_tee_shm_alloc(struct fp5_tee *t, size_t size, struct fp5_shm *s)
{
	struct tee_ioctl_shm_alloc_data d;
	int fd;
	void *mem;

	memset(s, 0, sizeof(*s));
	s->fd = -1;
	memset(&d, 0, sizeof(d));
	d.size = size;
	fd = ioctl(t->fd, TEE_IOC_SHM_ALLOC, &d);
	if (fd < 0)
		return -1;
	mem = mmap(NULL, (size_t)d.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (mem == MAP_FAILED) {
		close(fd);
		return -1;
	}
	s->fd = fd;
	s->id = (uint32_t)d.id;
	s->size = (size_t)d.size;
	s->mem = mem;
	return 0;
}

void fp5_tee_shm_free(struct fp5_shm *s)
{
	if (s->mem && s->mem != MAP_FAILED)
		munmap(s->mem, s->size);
	if (s->fd >= 0)
		close(s->fd);
	s->mem = NULL;
	s->fd = -1;
}

int fp5_tee_supp_recv(struct fp5_tee *t, void *ubuf, size_t ubuf_len,
		      struct fp5_supp_req *req)
{
	struct fp5_supp_recv_ioctl arg;
	struct tee_ioctl_buf_data buf;
	uint32_t i;

	memset(&arg, 0, sizeof(arg));
	arg.num_params = 8;
	arg.params[0].attr = FP5_VAL_INOUT | FP5_META;
	arg.params[0].a = (uint64_t)(uintptr_t)ubuf;
	arg.params[0].b = ubuf_len;
	buf.buf_ptr = (uint64_t)(uintptr_t)&arg;
	buf.buf_len = sizeof(arg);
	if (ioctl(t->fd, TEE_IOC_SUPPL_RECV, &buf))
		return -1;
	if (arg.num_params > 8)
		arg.num_params = 8;
	req->func = arg.func;
	req->num_params = arg.num_params;
	req->object_id = arg.params[0].a;
	req->req_id = (uint32_t)arg.params[0].b;
	for (i = 0; i < 8; i++)
		req->params[i] = arg.params[i];
	return 0;
}

int fp5_tee_supp_send(struct fp5_tee *t, uint32_t err,
		      const struct tee_param_view *params, uint32_t n)
{
	struct fp5_supp_send_ioctl arg;
	struct tee_ioctl_buf_data buf;

	if (n > 8)
		return -1;
	memset(&arg, 0, sizeof(arg));
	arg.ret = err;
	arg.num_params = n;
	if (n)
		memcpy(arg.params, params, n * sizeof(*params));
	buf.buf_ptr = (uint64_t)(uintptr_t)&arg;
	buf.buf_len = sizeof(struct fp5_supp_send_ioctl) -
		      (8 - n) * sizeof(struct tee_param_view);
	/* Kernel checks size_add(header, n * param) <= buf_len, not equality. */
	if (buf.buf_len < 8 + n * sizeof(struct tee_param_view))
		buf.buf_len = 8 + n * sizeof(struct tee_param_view);
	return ioctl(t->fd, TEE_IOC_SUPPL_SEND, &buf) ? -1 : 0;
}
