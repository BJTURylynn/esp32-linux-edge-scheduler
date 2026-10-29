#define _POSIX_C_SOURCE 200809L
/* Central assignment only. Computation lives on the two registered devices. */
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <math.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define CLIENTS 16
#define RECORDS 512
#define MAX_REQUEST 512
#define MAX_OUTPUT (256u * 1024u)
#define MAX_FIELDS 16

typedef enum { QUEUED, DISPATCHING, RUNNING, SUCCEEDED, FAILED, UNKNOWN } TaskState;
static const char *task_names[] = {"queued", "dispatching", "running", "succeeded", "failed", "unknown"};
typedef struct {
    uint32_t id, seed, count, window, passes, retry_of, compute_us;
    TaskState state;
    int urgent, node;
    uint64_t enqueued, dispatched, accepted, finished;
    double mean, rms;
    char error[48];
} Task;
typedef struct {
    int fd, node, closing;
    char input[MAX_REQUEST + 2];
    size_t used;
    char *output;
    size_t output_size, sent;
    uint64_t opened;
} Client;
typedef struct {
    const char *device;
    int client, job;
    char session[17], error[48];
    uint64_t last_seen, last_ping;
} Node;
typedef struct {
    char key[24], value[80];
    int string;
} Field;
typedef struct { Field values[MAX_FIELDS]; size_t count; } Object;
typedef struct {
    Client clients[CLIENTS];
    Node nodes[2];
    Task tasks[RECORDS];
    size_t total, capacity;
    uint64_t rejected;
    int priority, next_node, running, dirty, fatal;
    double accept_timeout, task_timeout;
    char run_id[17], directory[2048];
    FILE *events, *csv;
} Server;
static Server server;
static volatile sig_atomic_t stopping;

static uint64_t now_ns(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) abort();
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}
static void stop_signal(int signum) { (void)signum; stopping = 1; }
static void copy_text(char *to, size_t size, const char *from) {
    (void)snprintf(to, size, "%s", from);
}
static int random_tag(char out[17]) {
    uint64_t bytes;
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd < 0) return 0;
    unsigned char *p = (unsigned char *)&bytes;
    size_t received = 0;
    while (received < sizeof(bytes)) {
        ssize_t n = read(fd, p + received, sizeof(bytes) - received);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { close(fd); return 0; }
        received += (size_t)n;
    }
    close(fd);
    (void)snprintf(out, 17, "%016" PRIx64, bytes);
    return 1;
}

/* Flat bounded schema. No nested structures, arbitrary text or string escapes
 * are needed by this protocol. Numbers still obey JSON lexical grammar. */
static void space(const char **p) { while (**p == ' ' || **p == '\t' || **p == '\r') ++*p; }
static int quoted(const char **p, char *out, size_t size) {
    space(p);
    if (*(*p)++ != '"') return 0;
    size_t n = 0;
    while (**p && **p != '"') {
        unsigned char c = (unsigned char)*(*p)++;
        if (c < 32 || c > 126 || c == '\\' || n + 1 >= size) return 0;
        out[n++] = (char)c;
    }
    if (**p != '"') return 0;
    ++*p; out[n] = '\0'; return 1;
}
static int json_number(const char **p, char *out, size_t size) {
    const char *start = *p;
    if (**p == '-') ++*p;
    if (**p == '0') ++*p;
    else {
        if (**p < '1' || **p > '9') return 0;
        while (isdigit((unsigned char)**p)) ++*p;
    }
    if (**p == '.') {
        ++*p;
        if (!isdigit((unsigned char)**p)) return 0;
        while (isdigit((unsigned char)**p)) ++*p;
    }
    if (**p == 'e' || **p == 'E') {
        ++*p;
        if (**p == '+' || **p == '-') ++*p;
        if (!isdigit((unsigned char)**p)) return 0;
        while (isdigit((unsigned char)**p)) ++*p;
    }
    size_t length = (size_t)(*p - start);
    if (length == 0 || length >= size) return 0;
    memcpy(out, start, length); out[length] = '\0'; return 1;
}
static int parse(const char *p, Object *object) {
    object->count = 0; space(&p);
    if (*p++ != '{') return 0;
    for (;;) {
        if (object->count == MAX_FIELDS) return 0;
        Field *field = &object->values[object->count];
        if (!quoted(&p, field->key, sizeof(field->key))) return 0;
        for (size_t i = 0; i < object->count; ++i)
            if (!strcmp(object->values[i].key, field->key)) return 0;
        space(&p); if (*p++ != ':') return 0; space(&p);
        field->string = *p == '"';
        if (field->string ? !quoted(&p, field->value, sizeof(field->value)) :
                            !json_number(&p, field->value, sizeof(field->value))) return 0;
        ++object->count; space(&p);
        if (*p == '}') { ++p; space(&p); return *p == '\0'; }
        if (*p++ != ',') return 0;
    }
}
static const Field *get(const Object *o, const char *name) {
    for (size_t i = 0; i < o->count; ++i) if (!strcmp(o->values[i].key, name)) return &o->values[i];
    return NULL;
}
static const char *text(const Object *o, const char *name) {
    const Field *f = get(o, name); return f && f->string ? f->value : NULL;
}
static int integer(const Object *o, const char *name, uint32_t low, uint32_t high, uint32_t *out) {
    const Field *f = get(o, name);
    if (!f || f->string || !f->value[0]) return 0;
    uint64_t value = 0;
    for (const char *p = f->value; *p; ++p) {
        if (*p < '0' || *p > '9') return 0;
        value = value * 10u + (unsigned)(*p - '0');
        if (value > high) return 0;
    }
    if (value < low) return 0;
    *out = (uint32_t)value; return 1;
}
static int real_number(const Object *o, const char *name, double low, double high, double *out) {
    const Field *f = get(o, name);
    if (!f || f->string) return 0;
    char *end;
    errno = 0; double value = strtod(f->value, &end);
    if (*end || errno || !isfinite(value) || value < low || value > high) return 0;
    *out = value; return 1;
}
static int fields(const Object *o, size_t count, ...) {
    if (o->count != count) return 0;
    va_list names; va_start(names, count);
    int valid = 1;
    for (size_t i = 0; i < count; ++i) if (!get(o, va_arg(names, const char *))) valid = 0;
    va_end(names); return valid;
}
static size_t queued(const Server *s) {
    size_t n = 0;
    for (size_t i = 0; i < s->total; ++i) if (s->tasks[i].state == QUEUED) ++n;
    return n;
}
static void event(Server *s, const char *name, const char *device, uint32_t id, const char *reason) {
    if (fprintf(s->events, "{\"event\":\"%s\",\"central_ns\":%" PRIu64 ",\"device\":\"%s\",\"id\":%u,\"reason\":\"%s\"}\n",
                name, now_ns(), device ? device : "", id, reason ? reason : "") < 0 || fflush(s->events)) {
        s->fatal = 1; stopping = 1;
    }
    s->dirty = 1;
}
static void terminal(Server *s, Task *task, TaskState state, const char *reason) {
    task->state = state; task->finished = now_ns();
    copy_text(task->error, sizeof(task->error), reason ? reason : "");
    const char *device = task->node < 0 ? "" : s->nodes[task->node].device;
    if (fprintf(s->csv, "%u,%s,%s,%s,%u,%u,%u,%u,%u,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%.9g,%.9g,%u,%s\n",
        task->id, device, task->urgent ? "urgent" : "normal", task_names[state], task->seed,
        task->count, task->window, task->passes, task->retry_of, task->enqueued, task->dispatched,
        task->accepted, task->finished, task->mean, task->rms, task->compute_us, task->error) < 0 || fflush(s->csv)) {
        s->fatal = 1; stopping = 1;
    }
    event(s, task_names[state], device, task->id, reason);
}
static void disconnect_client(Server *s, int index, const char *reason) {
    Client *c = &s->clients[index];
    if (c->fd < 0) return;
    if (c->node >= 0) {
        Node *node = &s->nodes[c->node];
        if (node->job >= 0) {
            terminal(s, &s->tasks[node->job], UNKNOWN, reason);
            node->job = -1;
        }
        node->client = -1;
        copy_text(node->error, sizeof(node->error), reason);
        event(s, "disconnected", node->device, 0, reason);
    }
    close(c->fd); free(c->output); memset(c, 0, sizeof(*c));
    c->fd = c->node = -1;
}
static int output(Client *client, const char *data, size_t length) {
    if (client->output || length > MAX_OUTPUT) return 0;
    client->output = malloc(length);
    if (!client->output) return 0;
    memcpy(client->output, data, length); client->output_size = length; client->sent = 0;
    return 1;
}
static int reply(Client *c, const char *value) { c->closing = 1; return output(c, value, strlen(value)); }
static void error_reply(Server *s, int index, const char *reason) {
    char response[128];
    int n = snprintf(response, sizeof(response), "{\"ok\":false,\"error\":\"%s\"}\n", reason);
    if (n < 0 || (size_t)n >= sizeof(response) || !reply(&s->clients[index], response))
        disconnect_client(s, index, reason);
}
static const char *node_state(const Server *s, int index) {
    const Node *n = &s->nodes[index];
    if (n->client < 0) return n->error[0] ? "error" : "offline";
    return n->job < 0 ? "idle" : task_names[s->tasks[n->job].state];
}
static char *state_json(const Server *s, size_t *size) {
    char *buffer = NULL; FILE *f = open_memstream(&buffer, size);
    if (!f) return NULL;
    size_t counts[6] = {0};
    for (size_t i = 0; i < s->total; ++i) ++counts[s->tasks[i].state];
    fprintf(f, "{\"ok\":true,\"v\":2,\"running\":%s,\"run_id\":\"%s\",\"policy\":\"%s\",\"queue_depth\":%zu,\"queue_capacity\":%zu,\"record_capacity\":%d,\"counts\":{",
        s->running ? "true" : "false", s->run_id, s->priority ? "priority" : "fifo", queued(s), s->capacity, RECORDS);
    for (size_t i = 0; i < 6; ++i) fprintf(f, "%s\"%s\":%zu", i ? "," : "", task_names[i], counts[i]);
    fprintf(f, ",\"rejected\":%" PRIu64 "},\"nodes\":[", s->rejected);
    for (int i = 0; i < 2; ++i) {
        const Node *n = &s->nodes[i];
        fprintf(f, "%s{\"device\":\"%s\",\"state\":\"%s\",\"task_id\":", i ? "," : "", n->device, node_state(s, i));
        if (n->job < 0) fputs("null", f); else fprintf(f, "%u", s->tasks[n->job].id);
        fprintf(f, ",\"last_error\":%s", n->error[0] ? "\"" : "null");
        if (n->error[0]) fprintf(f, "%s\"", n->error);
        fputc('}', f);
    }
    fputs("],\"tasks\":[", f);
    for (size_t i = 0; i < s->total; ++i) {
        const Task *t = &s->tasks[i];
        fprintf(f, "%s{\"id\":%u,\"priority\":\"%s\",\"seed\":%u,\"count\":%u,\"window\":%u,\"passes\":%u,\"state\":\"%s\",\"device\":",
            i ? "," : "", t->id, t->urgent ? "urgent" : "normal", t->seed, t->count, t->window, t->passes, task_names[t->state]);
        if (t->node < 0) fputs("null", f); else fprintf(f, "\"%s\"", s->nodes[t->node].device);
        fputs(",\"retry_of\":", f); if (t->retry_of) fprintf(f, "%u", t->retry_of); else fputs("null", f);
        fputs(",\"mean\":", f); if (t->state == SUCCEEDED) fprintf(f, "%.9g", t->mean); else fputs("null", f);
        fputs(",\"rms\":", f); if (t->state == SUCCEEDED) fprintf(f, "%.9g", t->rms); else fputs("null", f);
        fputs(",\"compute_us\":", f); if (t->state == SUCCEEDED) fprintf(f, "%u", t->compute_us); else fputs("null", f);
        fputs(",\"wait_ms\":", f); if (t->dispatched) fprintf(f, "%.6f", (double)(t->dispatched - t->enqueued) / 1e6); else fputs("null", f);
        fputs(",\"roundtrip_ms\":", f); if (t->dispatched && t->finished) fprintf(f, "%.6f", (double)(t->finished - t->dispatched) / 1e6); else fputs("null", f);
        fputs(",\"error\":", f); if (t->error[0]) fprintf(f, "\"%s\"", t->error); else fputs("null", f);
        fputc('}', f);
    }
    fputs("]}\n", f);
    if (fclose(f)) { free(buffer); return NULL; }
    if (*size > MAX_OUTPUT) { free(buffer); return NULL; }
    return buffer;
}
static int save_state(Server *s) {
    if (!s->dirty) return 1;
    char path[2200], temporary[2200]; size_t size;
    char *data = state_json(s, &size);
    if (!data) return 0;
    snprintf(path, sizeof(path), "%s/status.json", s->directory);
    snprintf(temporary, sizeof(temporary), "%s/.status-XXXXXX", s->directory);
    int fd = mkstemp(temporary); int ok = fd >= 0;
    if (ok) {
        size_t written = 0;
        while (written < size) {
            ssize_t n = write(fd, data + written, size - written);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) { ok = 0; break; }
            written += (size_t)n;
        }
        if (close(fd)) ok = 0;
        if (ok && rename(temporary, path)) ok = 0;
        if (!ok) unlink(temporary);
    }
    free(data); if (ok) s->dirty = 0; return ok;
}
static void submit(Server *s, int index, const Object *object, uint32_t retry_of) {
    Task task; memset(&task, 0, sizeof(task)); task.node = -1;
    const char *priority = text(object, "priority");
    if (retry_of) {
        if (retry_of > s->total || (s->tasks[retry_of-1].state != FAILED && s->tasks[retry_of-1].state != UNKNOWN)) {
            error_reply(s, index, "not_retryable"); return;
        }
        const Task *previous = &s->tasks[retry_of - 1];
        task.seed = previous->seed; task.count = previous->count; task.window = previous->window;
        task.passes = previous->passes; task.urgent = previous->urgent; task.retry_of = retry_of;
    } else if (!(get(object, "run_id") ?
                 fields(object, 8, "v", "type", "priority", "seed", "count", "window", "passes", "run_id") :
                 fields(object, 7, "v", "type", "priority", "seed", "count", "window", "passes")) ||
               !priority || (strcmp(priority, "normal") && strcmp(priority, "urgent")) ||
               !integer(object, "seed", 0, UINT32_MAX, &task.seed) ||
               !integer(object, "count", 1, 256, &task.count) ||
               !integer(object, "window", 1, 16, &task.window) ||
               !integer(object, "passes", 1, 1000, &task.passes)) {
        error_reply(s, index, "invalid_request"); return;
    } else task.urgent = !strcmp(priority, "urgent");
    if (!s->running || queued(s) >= s->capacity || s->total >= RECORDS) {
        ++s->rejected;
        error_reply(s, index, !s->running ? "stopping" : s->total >= RECORDS ? "record_limit" : "queue_full");
        event(s, "rejected", NULL, 0, "admission_limit"); return;
    }
    task.id = (uint32_t)s->total + 1; task.enqueued = now_ns(); task.state = QUEUED;
    s->tasks[s->total++] = task;
    event(s, "queued", NULL, task.id, retry_of ? "manual_retry" : "submit");
    char response[80]; snprintf(response, sizeof(response), "{\"ok\":true,\"task_id\":%u}\n", task.id);
    if (!reply(&s->clients[index], response)) disconnect_client(s, index, "send_error");
}
static void worker_message(Server *s, int index, const Object *object, const char *type) {
    Client *c = &s->clients[index]; Node *node = &s->nodes[c->node];
    const char *device = text(object, "device"), *session = text(object, "session");
    uint32_t id = 0;
    if (!device || !session || strcmp(device, node->device) || strcmp(session, node->session)) {
        event(s, "ignored_result", node->device, 0, "identity_or_session"); return;
    }
    if (!strcmp(type, "pong")) {
        if (!fields(object, 4, "v", "type", "device", "session")) {
            disconnect_client(s, index, "invalid_message"); return;
        }
        node->last_seen = now_ns(); return;
    }
    if (!integer(object, "id", 1, UINT32_MAX, &id) || node->job < 0 || s->tasks[node->job].id != id) {
        event(s, "ignored_result", node->device, id, "unexpected_task"); return;
    }
    Task *task = &s->tasks[node->job];
    if (!strcmp(type, "accept")) {
        if (!fields(object, 5, "v", "type", "device", "session", "id") || task->state != DISPATCHING) {
            event(s, "ignored_result", node->device, id, "unexpected_accept"); return;
        }
        task->accepted = now_ns(); task->state = RUNNING; node->last_seen = task->accepted;
        event(s, "accepted", node->device, id, NULL); return;
    }
    if (!strcmp(type, "error")) {
        const char *reason = text(object, "reason");
        if (!fields(object, 6, "v", "type", "device", "session", "id", "reason") || !reason ||
            (strcmp(reason, "invalid_task") && strcmp(reason, "compute_error") && strcmp(reason, "busy") &&
             strcmp(reason, "duplicate_task") && strcmp(reason, "nonfinite_result"))) {
            disconnect_client(s, index, "invalid_message"); return;
        }
        terminal(s, task, FAILED, reason); node->job = -1; node->last_seen = now_ns(); return;
    }
    if (!strcmp(type, "result")) {
        double mean, rms; uint32_t micros;
        if (!fields(object, 8, "v", "type", "device", "session", "id", "mean", "rms", "compute_us") ||
            task->state != RUNNING || !real_number(object, "mean", -100.001, 100.001, &mean) ||
            !real_number(object, "rms", 0, 100.001, &rms) || !integer(object, "compute_us", 0, UINT32_MAX, &micros)) {
            event(s, "ignored_result", node->device, id, "invalid_or_unaccepted_result"); return;
        }
        task->mean = mean; task->rms = rms; task->compute_us = micros;
        terminal(s, task, SUCCEEDED, NULL); node->job = -1; node->last_seen = now_ns(); return;
    }
    disconnect_client(s, index, "invalid_message");
}
static void message(Server *s, int index, const char *line, size_t length) {
    Client *c = &s->clients[index];
    Object object; uint32_t version;
    if (!length || !parse(line, &object) || !integer(&object, "v", 2, 2, &version)) {
        if (c->node >= 0) disconnect_client(s, index, "invalid_message"); else error_reply(s, index, "invalid_request");
        return;
    }
    const char *type = text(&object, "type");
    if (!type) { disconnect_client(s, index, "invalid_message"); return; }
    if ((c->node >= 0 || !strcmp(type, "hello")) && length > 256) {
        disconnect_client(s, index, "oversized_frame"); return;
    }
    if (c->node >= 0) { worker_message(s, index, &object, type); return; }
    if (!strcmp(type, "hello")) {
        const char *device = text(&object, "device"); int node_index = -1;
        for (int i = 0; i < 2; ++i) if (device && !strcmp(device, s->nodes[i].device)) node_index = i;
        if (!fields(&object, 3, "v", "type", "device") || node_index < 0 || s->nodes[node_index].client >= 0) {
            error_reply(s, index, "invalid_or_duplicate_device"); return;
        }
        Node *node = &s->nodes[node_index];
        if (!random_tag(node->session)) { disconnect_client(s, index, "random_error"); s->fatal = 1; stopping = 1; return; }
        node->client = index; node->job = -1; node->error[0] = '\0';
        node->last_seen = node->last_ping = now_ns(); c->node = node_index;
        char welcome[100]; snprintf(welcome, sizeof(welcome), "{\"v\":2,\"type\":\"welcome\",\"session\":\"%s\"}\n", node->session);
        if (!output(c, welcome, strlen(welcome))) disconnect_client(s, index, "send_error");
        event(s, "connected", node->device, 0, NULL); return;
    }
    if (!strcmp(type, "state") && fields(&object, 2, "v", "type")) {
        size_t size; char *json = state_json(s, &size);
        if (!json || !output(c, json, size)) disconnect_client(s, index, "state_error");
        else c->closing = 1;
        free(json); return;
    }
    if ((!strcmp(type, "submit") || !strcmp(type, "retry")) && get(&object, "run_id")) {
        const char *run = text(&object, "run_id");
        /* Task numbers restart with each daemon run. Browser requests carry
         * this opaque epoch so an old page cannot mutate a newer run's job. */
        if (!run || strcmp(run, s->run_id)) { error_reply(s, index, "stale_run"); return; }
    }
    if (!strcmp(type, "submit")) { submit(s, index, &object, 0); return; }
    if (!strcmp(type, "retry")) {
        uint32_t id;
        if (!(get(&object, "run_id") ? fields(&object, 4, "v", "type", "id", "run_id") :
              fields(&object, 3, "v", "type", "id")) || !integer(&object, "id", 1, RECORDS, &id)) error_reply(s, index, "invalid_request");
        else submit(s, index, &object, id);
        return;
    }
    error_reply(s, index, "invalid_request");
}
static void schedule(Server *s) {
    for (int round = 0; round < 2; ++round) {
        int chosen_node = -1;
        for (int i = 0; i < 2; ++i) {
            int n = (s->next_node + i) % 2; Node *node = &s->nodes[n];
            if (node->client >= 0 && node->job < 0 && !s->clients[node->client].output) { chosen_node = n; break; }
        }
        if (chosen_node < 0) return;
        int chosen_task = -1;
        for (size_t i = 0; i < s->total; ++i) {
            Task *t = &s->tasks[i];
            if (t->state != QUEUED) continue;
            if (chosen_task < 0) chosen_task = (int)i;
            if (!s->priority || t->urgent) { chosen_task = (int)i; break; }
        }
        if (chosen_task < 0) return;
        Node *node = &s->nodes[chosen_node]; Task *task = &s->tasks[chosen_task];
        char command[256];
        int n = snprintf(command, sizeof(command), "{\"v\":2,\"type\":\"task\",\"session\":\"%s\",\"id\":%u,\"seed\":%u,\"count\":%u,\"window\":%u,\"passes\":%u}\n",
            node->session, task->id, task->seed, task->count, task->window, task->passes);
        if (n < 0 || (size_t)n >= sizeof(command) || !output(&s->clients[node->client], command, (size_t)n)) {
            disconnect_client(s, node->client, "send_error"); continue;
        }
        node->job = chosen_task; task->node = chosen_node; task->state = DISPATCHING; task->dispatched = now_ns();
        s->next_node = (chosen_node + 1) % 2;
        event(s, "dispatched", node->device, task->id, NULL);
    }
}
static void deadlines(Server *s) {
    uint64_t now = now_ns();
    for (int i = 0; i < CLIENTS; ++i) {
        Client *c = &s->clients[i];
        if (c->fd < 0) continue;
        if (c->node < 0) {
            if ((double)(now - c->opened) / 1e9 > 5) disconnect_client(s, i, "request_timeout");
            continue;
        }
        Node *node = &s->nodes[c->node];
        if (node->job >= 0) {
            Task *t = &s->tasks[node->job];
            double elapsed = (double)(now - t->dispatched) / 1e9;
            if (elapsed > s->task_timeout || (t->state == DISPATCHING && elapsed > s->accept_timeout))
                disconnect_client(s, i, t->state == DISPATCHING ? "accept_timeout" : "task_timeout");
        } else if ((double)(now - node->last_seen) / 1e9 > 5) disconnect_client(s, i, "heartbeat_timeout");
        else if (!c->output && (double)(now - node->last_ping) / 1e9 >= 1) {
            char ping[100]; snprintf(ping, sizeof(ping), "{\"v\":2,\"type\":\"ping\",\"session\":\"%s\"}\n", node->session);
            if (!output(c, ping, strlen(ping))) disconnect_client(s, i, "send_error");
            node->last_ping = now;
        }
    }
}
static int nonblocking(int fd) { int flags = fcntl(fd, F_GETFL, 0); return flags >= 0 && !fcntl(fd, F_SETFL, flags | O_NONBLOCK); }
static void receive_client(Server *s, int index) {
    Client *c = &s->clients[index]; char bytes[1024];
    ssize_t count = recv(c->fd, bytes, sizeof(bytes), 0);
    if (!count) { disconnect_client(s, index, "disconnected"); return; }
    if (count < 0) { if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) disconnect_client(s, index, "receive_error"); return; }
    for (ssize_t n = 0; n < count && c->fd >= 0 && !c->closing; ++n) {
        char ch = bytes[n];
        if (ch == '\n') {
            if (c->used && c->input[c->used - 1] == '\r') --c->used;
            c->input[c->used] = '\0';
            size_t length = c->used; c->used = 0;
            message(s, index, c->input, length);
        } else if ((unsigned char)ch < 32 || (unsigned char)ch > 126) {
            if (ch == '\r' || ch == '\t') {
                if (c->used >= (c->node >= 0 ? 257u : MAX_REQUEST)) { disconnect_client(s, index, "oversized_frame"); return; }
                c->input[c->used++] = ch;
            } else { disconnect_client(s, index, "invalid_byte"); return; }
        } else {
            size_t limit = c->node >= 0 ? 256u : MAX_REQUEST;
            if (c->used >= limit) { disconnect_client(s, index, "oversized_frame"); return; }
            c->input[c->used++] = ch;
        }
    }
}
static void flush_client(Server *s, int index) {
    Client *c = &s->clients[index];
    if (!c->output) return;
    ssize_t count = send(c->fd, c->output + c->sent, c->output_size - c->sent, MSG_NOSIGNAL);
    if (count > 0) c->sent += (size_t)count;
    else if (count == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) { disconnect_client(s, index, "send_error"); return; }
    if (c->sent == c->output_size) {
        free(c->output); c->output = NULL; c->sent = c->output_size = 0;
        if (c->closing) disconnect_client(s, index, "response_complete");
    }
}
static int serve(Server *s, const char *bind_host, unsigned port) {
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) return 0;
    int yes = 1; setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    struct sockaddr_in address; memset(&address, 0, sizeof(address)); address.sin_family = AF_INET; address.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, bind_host, &address.sin_addr) != 1 || bind(listener, (struct sockaddr *)&address, sizeof(address)) || listen(listener, CLIENTS) || !nonblocking(listener)) { close(listener); return 0; }
    s->running = s->dirty = 1;
    if (!save_state(s)) { close(listener); return 0; }
    printf("{\"event\":\"ready\",\"port\":%u,\"run_id\":\"%s\"}\n", port, s->run_id); fflush(stdout);
    while (!stopping) {
        struct pollfd pollers[CLIENTS + 1];
        pollers[0] = (struct pollfd){listener, POLLIN, 0};
        for (int i = 0; i < CLIENTS; ++i) {
            Client *c = &s->clients[i];
            pollers[i + 1] = (struct pollfd){c->fd, (short)((c->closing ? 0 : POLLIN) | (c->output ? POLLOUT : 0)), 0};
        }
        int ready = poll(pollers, CLIENTS + 1, 20);
        if (ready < 0 && errno != EINTR) { s->fatal = 1; break; }
        if (pollers[0].revents & POLLIN) {
            int fd = accept(listener, NULL, NULL); int slot = -1;
            for (int i = 0; i < CLIENTS; ++i) if (s->clients[i].fd < 0) { slot = i; break; }
            if (fd >= 0) {
                if (slot < 0 || !nonblocking(fd)) close(fd);
                else { Client *c = &s->clients[slot]; c->fd = fd; c->node = -1; c->opened = now_ns(); }
            }
        }
        for (int i = 0; i < CLIENTS; ++i) {
            if (s->clients[i].fd < 0 || pollers[i + 1].fd != s->clients[i].fd) continue;
            short events = pollers[i + 1].revents;
            if (events & POLLIN) receive_client(s, i);
            if (s->clients[i].fd >= 0 && events & POLLOUT) flush_client(s, i);
            if (s->clients[i].fd >= 0 && events & (POLLERR | POLLNVAL)) disconnect_client(s, i, "disconnected");
            /* A management peer may half-close its write side after the one
             * request and still read the response. Finish that bounded write. */
            if (s->clients[i].fd >= 0 && events & POLLHUP &&
                !(s->clients[i].node < 0 && s->clients[i].closing && s->clients[i].output))
                disconnect_client(s, i, "disconnected");
        }
        deadlines(s); schedule(s);
        if (!save_state(s)) { s->fatal = 1; break; }
    }
    s->running = 0;
    for (int i = 0; i < CLIENTS; ++i) disconnect_client(s, i, "server_stopped");
    for (size_t i = 0; i < s->total; ++i) if (s->tasks[i].state == QUEUED) terminal(s, &s->tasks[i], FAILED, "server_stopped");
    s->dirty = 1;
    if (!save_state(s)) s->fatal = 1;
    close(listener); return !s->fatal;
}
static int parse_unsigned(const char *text_value, unsigned low, unsigned high, unsigned *out) {
    if (!text_value[0]) return 0;
    uint64_t value = 0;
    for (const char *p = text_value; *p; ++p) {
        if (*p < '0' || *p > '9') return 0;
        value = value * 10u + (unsigned)(*p - '0');
        if (value > high) return 0;
    }
    if (value < low) return 0;
    *out = (unsigned)value; return 1;
}
int main(int argc, char **argv) {
    Server *s = &server;
    const char *bind_host = "127.0.0.1", *directory = NULL;
    unsigned port = 9100, capacity = 64;
    s->priority = 1; s->accept_timeout = 3; s->task_timeout = 30;
    for (int i = 0; i < CLIENTS; ++i) s->clients[i].fd = s->clients[i].node = -1;
    s->nodes[0] = (Node){.device="uno-01", .client=-1, .job=-1};
    s->nodes[1] = (Node){.device="esp32c3-01", .client=-1, .job=-1};
    for (int i = 1; i < argc; i += 2) {
        if (i + 1 >= argc) { fprintf(stderr, "missing option value\n"); return 2; }
        const char *name = argv[i], *value = argv[i + 1];
        if (!strcmp(name, "--bind")) bind_host = value;
        else if (!strcmp(name, "--output")) directory = value;
        else if (!strcmp(name, "--port")) { if (!parse_unsigned(value, 1, 65535, &port)) return 2; }
        else if (!strcmp(name, "--capacity")) { if (!parse_unsigned(value, 1, 64, &capacity)) return 2; }
        else if (!strcmp(name, "--policy")) { if (strcmp(value, "fifo") && strcmp(value, "priority")) return 2; s->priority = !strcmp(value, "priority"); }
        else if (!strcmp(name, "--accept-timeout") || !strcmp(name, "--task-timeout")) {
            char *end; double number = strtod(value, &end);
            if (!value[0] || *end || !isfinite(number) || number < .05 || number > 600) return 2;
            if (!strcmp(name, "--accept-timeout")) s->accept_timeout = number; else s->task_timeout = number;
        } else { fprintf(stderr, "unknown option\n"); return 2; }
    }
    if (!directory || strlen(directory) >= sizeof(s->directory) || !random_tag(s->run_id)) return 2;
    s->capacity = capacity; copy_text(s->directory, sizeof(s->directory), directory);
    if (mkdir(directory, 0700)) { perror("create new output directory"); return 2; }
    char path[2200];
    snprintf(path, sizeof(path), "%s/events.jsonl", directory); s->events = fopen(path, "wx");
    snprintf(path, sizeof(path), "%s/tasks.csv", directory); s->csv = fopen(path, "wx");
    if (!s->events || !s->csv) { perror("create evidence"); if (s->events) fclose(s->events); if (s->csv) fclose(s->csv); return 2; }
    fputs("id,device,priority,state,seed,count,window,passes,retry_of,enqueued_ns,dispatched_ns,accepted_ns,finished_ns,mean,rms,compute_us,error\n", s->csv);
    fflush(s->csv);
    signal(SIGINT, stop_signal); signal(SIGTERM, stop_signal); signal(SIGPIPE, SIG_IGN);
    int ok = serve(s, bind_host, port);
    if (fclose(s->csv)) ok = 0;
    if (fclose(s->events)) ok = 0;
    if (!ok) { perror("dispatcher failed"); return 1; }
    return 0;
}
