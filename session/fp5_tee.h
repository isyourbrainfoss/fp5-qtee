#ifndef FP5_TEE_H
#define FP5_TEE_H

#include <stddef.h>
#include <stdint.h>

#define FP5_TEE_OBJ_NULL ((uint64_t)-1)

struct fp5_tee {
	int fd;
};

struct tee_param_view {
	uint64_t attr;
	uint64_t a;
	uint64_t b;
	uint64_t c;
};

int fp5_tee_open(struct fp5_tee *t, const char *dev);
void fp5_tee_close(struct fp5_tee *t);

/* QTEE result in *qret. Returns ioctl status (0 or -1). */
int fp5_tee_invoke(struct fp5_tee *t, uint64_t id, uint32_t op,
		   struct tee_param_view *params, uint32_t n, uint32_t *qret);

int fp5_tee_client_env(struct fp5_tee *t, uint64_t *env, uint32_t *qret);
int fp5_tee_open_service(struct fp5_tee *t, uint64_t env, uint32_t uid,
			 uint64_t *svc, uint32_t *qret);

/*
 * Loader op 1. name_len is the byte count sent (no trailing NUL required).
 * On success *app is the trustlet object.
 */
int fp5_tee_load_buffer(struct fp5_tee *t, uint64_t loader,
			const void *image, size_t image_len,
			const char *name, size_t name_len,
			char *dist, size_t dist_cap,
			uint64_t *app, uint32_t *qret);

#define FP5_OBJ_TEE 1u
#define FP5_OBJ_USER 2u
#define FP5_OBJ_MEM 4u

struct fp5_shm {
	int fd;
	uint32_t id;
	size_t size;
	void *mem;
};

struct fp5_supp_req {
	uint32_t func;
	uint32_t num_params;
	uint32_t req_id;
	uint64_t object_id;
	struct tee_param_view params[8];
};

int fp5_tee_shm_alloc(struct fp5_tee *t, size_t size, struct fp5_shm *s);
void fp5_tee_shm_free(struct fp5_shm *s);
int fp5_tee_supp_recv(struct fp5_tee *t, void *ubuf, size_t ubuf_len,
		      struct fp5_supp_req *req);
int fp5_tee_supp_send(struct fp5_tee *t, uint32_t err,
		      const struct tee_param_view *params, uint32_t n);

#endif
