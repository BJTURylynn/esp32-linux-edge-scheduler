#ifndef EDGE_H
#define EDGE_H

#include <stddef.h>
#include <stdint.h>

#define EDGE_MAX_SAMPLES 8192u
#define EDGE_MAX_LINE 262144u
#define EDGE_MAX_DEVICE 32u
#define EDGE_MAX_SEQ UINT64_C(9007199254740991)

typedef enum { EDGE_NORMAL = 0, EDGE_URGENT = 1 } EdgePriority;
typedef enum { EDGE_FIFO = 0, EDGE_PRIORITY = 1 } EdgePolicy;

typedef struct {
    uint64_t id, seq, received_ns, enqueued_ns, started_ns, finished_ns;
    char device_id[EDGE_MAX_DEVICE + 1];
    EdgePriority priority;
    size_t count;
    double samples[EDGE_MAX_SAMPLES];
    double rms, mean;
} EdgeTask;

/* Strict schema: exactly device_id, seq, priority, source, samples. */
int edge_parse(const char *json, size_t length, EdgeTask *task);
void edge_compute(EdgeTask *task, size_t window, unsigned passes);

typedef struct {
    EdgeTask **items;
    size_t length, capacity;
    EdgePolicy policy;
} EdgeQueue;
int edge_queue_init(EdgeQueue *queue, size_t capacity, EdgePolicy policy);
void edge_queue_destroy(EdgeQueue *queue);
int edge_queue_push(EdgeQueue *queue, EdgeTask *task);
EdgeTask *edge_queue_pop(EdgeQueue *queue);

/* The caller serializes access. Oversize frames are discarded through newline. */
typedef struct {
    char *data;
    size_t length;
    int dropping;
} EdgeFramer;
typedef int (*EdgeFrameFn)(const char *data, size_t length, const char *error,
                           void *context);
int edge_framer_init(EdgeFramer *framer);
void edge_framer_destroy(EdgeFramer *framer);
int edge_framer_feed(EdgeFramer *framer, const char *data, size_t length,
                     EdgeFrameFn callback, void *context);
int edge_framer_finish(EdgeFramer *framer, EdgeFrameFn callback, void *context);

#endif
