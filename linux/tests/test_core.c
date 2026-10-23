#include "edge.h"

#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *valid = "{\"device_id\":\"esp32-01\",\"seq\":1,"
    "\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1,2,3,4]}";

static void test_parser(void)
{
    EdgeTask *task = malloc(sizeof(*task));
    assert(task);
    assert(edge_parse(valid, strlen(valid), task));
    assert(task->count == 4 && task->seq == 1 && task->samples[3] == 4);
    const char *escaped = " {\"samples\":[-1.5e+2,0,1E-300],\"source\":\"synthetic\","
        "\"priority\":\"urgent\",\"seq\":9007199254740991,\"device_id\":\"esp32\\u002d01\"}\r ";
    assert(edge_parse(escaped, strlen(escaped), task));
    assert(task->seq == EDGE_MAX_SEQ && task->priority == EDGE_URGENT);
    assert(strcmp(task->device_id, "esp32-01") == 0 && task->samples[0] == -150);
    const char *invalid[] = {
        "", "{}", "[]", "null", "{", "{\"device_id\":\"x\"}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[]}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\",\"source\":\"sensor\",\"samples\":[1]}",
        "{\"device_id\":\"a\",\"seq\":-1,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1]}",
        "{\"device_id\":\"a\",\"seq\":9007199254740992,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1]}",
        "{\"device_id\":\"a\",\"seq\":00,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1]}",
        "{\"device_id\":\"a\",\"seq\":1.0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1]}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[NaN]}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1e400]}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1000001]}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[01]}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1,]}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"source\":\"synthetic\"}",
        "{\"device_id\":\"a,evil\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1]}",
        "{\"device_id\":\"\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1]}",
        "{\"device_id\":\"a\\u0000x\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1]}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"URGENT\",\"source\":\"synthetic\",\"samples\":[1]}",
        "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\",\"source\":\"synthetic\",\"samples\":[1],\"extra\":2}"
    };
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i)
        assert(!edge_parse(invalid[i], strlen(invalid[i]), task));
    /* Every truncation of a valid frame is invalid. */
    for (size_t i = 0; i < strlen(valid); ++i) assert(!edge_parse(valid, i, task));
    char *many = malloc(EDGE_MAX_LINE + 1);
    assert(many);
    size_t n = (size_t)sprintf(many, "{\"device_id\":\"a\",\"seq\":0,\"priority\":\"normal\","
                             "\"source\":\"synthetic\",\"samples\":[");
    for (size_t i = 0; i < EDGE_MAX_SAMPLES; ++i) {
        if (i) many[n++] = ',';
        many[n++] = '1';
    }
    many[n++] = ']'; many[n++] = '}';
    assert(edge_parse(many, n, task) && task->count == EDGE_MAX_SAMPLES);
    many[n - 2] = ','; many[n - 1] = '1'; many[n++] = ']'; many[n++] = '}';
    assert(!edge_parse(many, n, task));
    free(many);
    free(task);
}

static void test_computation(void)
{
    EdgeTask *task = malloc(sizeof(*task));
    assert(task && edge_parse(valid, strlen(valid), task));
    edge_compute(task, 2, 1);
    /* Filtered [1, 1.5, 2.5, 3.5]. */
    assert(fabs(task->mean - 2.125) < 1e-12);
    assert(fabs(task->rms - sqrt(21.75 / 4.0)) < 1e-12);
    double rms = task->rms;
    edge_compute(task, 2, 11);
    assert(task->rms == rms);
    edge_compute(task, 8, 1);
    assert(fabs(task->mean - 1.75) < 1e-12);
    edge_compute(task, 1, 1);
    assert(fabs(task->rms - sqrt(7.5)) < 1e-12);
    for (size_t i = 0; i < task->count; ++i) task->samples[i] = -1e6;
    edge_compute(task, 3, 2);
    assert(task->mean == -1e6 && task->rms == 1e6);
    free(task);
}

static EdgeTask *new_task(uint64_t id, EdgePriority priority)
{
    EdgeTask *task = calloc(1, sizeof(*task));
    assert(task);
    task->id = id;
    task->priority = priority;
    return task;
}

static void test_queue(void)
{
    for (int policy = EDGE_FIFO; policy <= EDGE_PRIORITY; ++policy) {
        EdgeQueue q;
        assert(edge_queue_init(&q, 4, (EdgePolicy)policy));
        assert(!edge_queue_pop(&q));
        assert(edge_queue_push(&q, new_task(1, EDGE_NORMAL)));
        EdgeTask *active = edge_queue_pop(&q);
        assert(active->id == 1); /* Running task stays outside the scheduler. */
        assert(edge_queue_push(&q, new_task(2, EDGE_NORMAL)));
        assert(edge_queue_push(&q, new_task(3, EDGE_URGENT)));
        assert(edge_queue_push(&q, new_task(4, EDGE_NORMAL)));
        assert(edge_queue_push(&q, new_task(5, EDGE_URGENT)));
        EdgeTask *extra = new_task(6, EDGE_URGENT);
        assert(!edge_queue_push(&q, extra)); free(extra);
        const uint64_t fifo[] = {2, 3, 4, 5}, priority[] = {3, 5, 2, 4};
        for (size_t i = 0; i < 4; ++i) {
            EdgeTask *task = edge_queue_pop(&q);
            assert(task->id == (policy == EDGE_FIFO ? fifo[i] : priority[i]));
            free(task);
        }
        assert(active->id == 1); free(active);
        assert(!edge_queue_pop(&q));
        edge_queue_destroy(&q);
    }
}

typedef struct { size_t messages, large, incomplete; char last[16]; } Frames;
static int record(const char *data, size_t length, const char *error, void *context)
{
    Frames *frames = context;
    if (error) {
        if (strcmp(error, "frame_too_large") == 0) ++frames->large;
        else { assert(strcmp(error, "incomplete_frame") == 0); ++frames->incomplete; }
    } else {
        ++frames->messages;
        size_t n = length < sizeof(frames->last) - 1 ? length : sizeof(frames->last) - 1;
        memcpy(frames->last, data, n); frames->last[n] = '\0';
    }
    return 1;
}

static void test_frames(void)
{
    EdgeFramer framer;
    Frames frames = {0};
    assert(edge_framer_init(&framer));
    assert(edge_framer_feed(&framer, "abc", 3, record, &frames));
    assert(frames.messages == 0);
    assert(edge_framer_feed(&framer, "d\ne\nf", 5, record, &frames));
    assert(frames.messages == 2 && strcmp(frames.last, "e") == 0);
    assert(edge_framer_finish(&framer, record, &frames));
    assert(frames.incomplete == 1);
    char *large = malloc(EDGE_MAX_LINE + 1u);
    assert(large); memset(large, 'x', EDGE_MAX_LINE + 1u);
    assert(edge_framer_feed(&framer, large, EDGE_MAX_LINE, record, &frames));
    assert(edge_framer_feed(&framer, "\n", 1, record, &frames));
    assert(frames.messages == 3 && !frames.large);
    assert(edge_framer_feed(&framer, large, EDGE_MAX_LINE + 1u, record, &frames));
    assert(frames.large == 1);
    assert(edge_framer_feed(&framer, "more\ngood\n", 10, record, &frames));
    assert(frames.messages == 4 && frames.large == 1 && strcmp(frames.last, "good") == 0);
    assert(edge_framer_feed(&framer, large, EDGE_MAX_LINE + 1u, record, &frames));
    assert(edge_framer_finish(&framer, record, &frames));
    assert(frames.large == 2 && frames.incomplete == 1); /* No double counting. */
    free(large);
    edge_framer_destroy(&framer);
}

int main(void)
{
    test_parser(); test_computation(); test_queue(); test_frames();
    puts("core tests: parser, compute, queue, framing passed");
    return 0;
}
