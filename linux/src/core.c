#include "edge.h"

#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

typedef struct { const char *at, *end; } Parser;

static void space(Parser *p)
{
    while (p->at < p->end && (*p->at == ' ' || *p->at == '\t' ||
           *p->at == '\r' || *p->at == '\n')) ++p->at;
}

static int take(Parser *p, char c)
{
    space(p);
    if (p->at == p->end || *p->at != c) return 0;
    ++p->at;
    return 1;
}

static int digit(char c) { return c >= '0' && c <= '9'; }
static int hex(char c)
{
    if (digit(c)) return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Schema strings are ASCII. Decode JSON escapes before schema validation. */
static int string(Parser *p, char *out, size_t capacity)
{
    size_t n = 0;
    if (!take(p, '"')) return 0;
    while (p->at < p->end) {
        unsigned char c = (unsigned char)*p->at++;
        if (c == '"') { out[n] = '\0'; return 1; }
        if (c == '\\') {
            if (p->at == p->end) return 0;
            c = (unsigned char)*p->at++;
            switch (c) {
            case '"': case '\\': case '/': break;
            case 'b': c = '\b'; break;
            case 'f': c = '\f'; break;
            case 'n': c = '\n'; break;
            case 'r': c = '\r'; break;
            case 't': c = '\t'; break;
            case 'u': {
                unsigned value = 0;
                for (unsigned i = 0; i < 4; ++i) {
                    if (p->at == p->end) return 0;
                    int h = hex(*p->at++);
                    if (h < 0) return 0;
                    value = value * 16u + (unsigned)h;
                }
                if (value == 0 || value > 127) return 0;
                c = (unsigned char)value;
                break;
            }
            default: return 0;
            }
        } else if (c < 32 || c > 127) return 0;
        if (n + 1 >= capacity || c == 0) return 0;
        out[n++] = (char)c;
    }
    return 0;
}

static int number(Parser *p, double *out)
{
    char small[128], *end = NULL;
    space(p);
    const char *begin = p->at;
    if (p->at < p->end && *p->at == '-') ++p->at;
    if (p->at == p->end) return 0;
    if (*p->at == '0') ++p->at;
    else {
        if (*p->at < '1' || *p->at > '9') return 0;
        while (p->at < p->end && digit(*p->at)) ++p->at;
    }
    if (p->at < p->end && *p->at == '.') {
        ++p->at;
        if (p->at == p->end || !digit(*p->at)) return 0;
        while (p->at < p->end && digit(*p->at)) ++p->at;
    }
    if (p->at < p->end && (*p->at == 'e' || *p->at == 'E')) {
        ++p->at;
        if (p->at < p->end && (*p->at == '+' || *p->at == '-')) ++p->at;
        if (p->at == p->end || !digit(*p->at)) return 0;
        while (p->at < p->end && digit(*p->at)) ++p->at;
    }
    size_t n = (size_t)(p->at - begin);
    if (n > EDGE_MAX_LINE) return 0;
    char *buffer = n < sizeof(small) ? small : malloc(n + 1u);
    if (!buffer) return 0;
    memcpy(buffer, begin, n);
    buffer[n] = '\0';
    errno = 0;
    *out = strtod(buffer, &end);
    int ok = end == buffer + n && isfinite(*out) && fabs(*out) <= 1e6;
    if (buffer != small) free(buffer);
    return ok;
}

static int sequence(Parser *p, uint64_t *value)
{
    space(p);
    if (p->at == p->end || !digit(*p->at)) return 0;
    const char *begin = p->at;
    *value = 0;
    while (p->at < p->end && digit(*p->at)) {
        unsigned d = (unsigned)(*p->at++ - '0');
        if (*value > (EDGE_MAX_SEQ - d) / 10u) return 0;
        *value = *value * 10u + d;
    }
    return p->at - begin == 1 || *begin != '0';
}

int edge_parse(const char *json, size_t length, EdgeTask *task)
{
    Parser p = { json, json + length };
    unsigned seen = 0;
    memset(task, 0, sizeof(*task));
    if (!take(&p, '{')) return 0;
    for (unsigned field = 0; field < 5; ++field) {
        char key[40], value[40];
        unsigned bit = 0;
        if (!string(&p, key, sizeof(key)) || !take(&p, ':')) return 0;
        if (strcmp(key, "device_id") == 0) {
            bit = 1;
            if (!string(&p, task->device_id, sizeof(task->device_id)) ||
                !task->device_id[0]) return 0;
            for (const char *c = task->device_id; *c; ++c)
                if (!digit(*c) && !(*c >= 'a' && *c <= 'z') &&
                    !(*c >= 'A' && *c <= 'Z') && *c != '_' && *c != '-') return 0;
        } else if (strcmp(key, "seq") == 0) {
            bit = 2;
            if (!sequence(&p, &task->seq)) return 0;
        } else if (strcmp(key, "priority") == 0) {
            bit = 4;
            if (!string(&p, value, sizeof(value))) return 0;
            if (strcmp(value, "normal") == 0) task->priority = EDGE_NORMAL;
            else if (strcmp(value, "urgent") == 0) task->priority = EDGE_URGENT;
            else return 0;
        } else if (strcmp(key, "source") == 0) {
            bit = 8;
            if (!string(&p, value, sizeof(value)) || strcmp(value, "synthetic") != 0)
                return 0;
        } else if (strcmp(key, "samples") == 0) {
            bit = 16;
            if (!take(&p, '[')) return 0;
            do {
                if (task->count >= EDGE_MAX_SAMPLES ||
                    !number(&p, &task->samples[task->count])) return 0;
                ++task->count;
                space(&p);
                if (p.at < p.end && *p.at == ']') break;
                if (!take(&p, ',')) return 0;
            } while (1);
            if (!take(&p, ']')) return 0;
        } else return 0;
        if (seen & bit) return 0;
        seen |= bit;
        if (field < 4 && !take(&p, ',')) return 0;
    }
    if (seen != 31 || !take(&p, '}')) return 0;
    space(&p);
    return p.at == p.end;
}

void edge_compute(EdgeTask *task, size_t window, unsigned passes)
{
    /* Volatile input reads guarantee real work on every repeated pass. */
    const volatile double *input = task->samples;
    for (unsigned pass = 0; pass < passes; ++pass) {
        double rolling = 0, squares = 0, total = 0;
        for (size_t i = 0; i < task->count; ++i) {
            rolling += input[i];
            if (i >= window) rolling -= input[i - window];
            size_t width = i + 1 < window ? i + 1 : window;
            double average = rolling / (double)width;
            squares += average * average;
            total += average;
        }
        task->rms = sqrt(squares / (double)task->count);
        task->mean = total / (double)task->count;
    }
}

int edge_queue_init(EdgeQueue *queue, size_t capacity, EdgePolicy policy)
{
    memset(queue, 0, sizeof(*queue));
    if (!capacity || capacity > SIZE_MAX / sizeof(*queue->items)) return 0;
    queue->items = calloc(capacity, sizeof(*queue->items));
    if (!queue->items) return 0;
    queue->capacity = capacity;
    queue->policy = policy;
    return 1;
}

void edge_queue_destroy(EdgeQueue *queue)
{
    for (size_t i = 0; i < queue->length; ++i) free(queue->items[i]);
    free(queue->items);
    memset(queue, 0, sizeof(*queue));
}

int edge_queue_push(EdgeQueue *queue, EdgeTask *task)
{
    if (queue->length == queue->capacity) return 0;
    queue->items[queue->length++] = task;
    return 1;
}

EdgeTask *edge_queue_pop(EdgeQueue *queue)
{
    size_t chosen = 0;
    if (!queue->length) return NULL;
    if (queue->policy == EDGE_PRIORITY)
        for (size_t i = 0; i < queue->length; ++i)
            if (queue->items[i]->priority == EDGE_URGENT) { chosen = i; break; }
    EdgeTask *task = queue->items[chosen];
    --queue->length;
    memmove(queue->items + chosen, queue->items + chosen + 1,
            (queue->length - chosen) * sizeof(*queue->items));
    return task;
}

int edge_framer_init(EdgeFramer *framer)
{
    memset(framer, 0, sizeof(*framer));
    framer->data = malloc(EDGE_MAX_LINE + 1u);
    return framer->data != NULL;
}

void edge_framer_destroy(EdgeFramer *framer)
{
    free(framer->data);
    memset(framer, 0, sizeof(*framer));
}

int edge_framer_feed(EdgeFramer *framer, const char *data, size_t length,
                     EdgeFrameFn callback, void *context)
{
    for (size_t i = 0; i < length; ++i) {
        if (data[i] == '\n') {
            if (!framer->dropping &&
                !callback(framer->data, framer->length, NULL, context)) return 0;
            framer->length = 0;
            framer->dropping = 0;
        } else if (!framer->dropping) {
            if (framer->length == EDGE_MAX_LINE) {
                framer->dropping = 1;
                framer->length = 0;
                if (!callback(NULL, 0, "frame_too_large", context)) return 0;
            } else framer->data[framer->length++] = data[i];
        }
    }
    return 1;
}

int edge_framer_finish(EdgeFramer *framer, EdgeFrameFn callback, void *context)
{
    int ok = 1;
    if (framer->length && !framer->dropping)
        ok = callback(NULL, 0, "incomplete_frame", context);
    framer->length = 0;
    framer->dropping = 0;
    return ok;
}
