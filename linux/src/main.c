#define _POSIX_C_SOURCE 200809L
#include "edge.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MAX_CLIENTS 16
#define PATH_BUFFER 4096

typedef struct {
    EdgeQueue queue;
    pthread_mutex_t mutex;
    pthread_cond_t ready;
    FILE *csv;
    char output[PATH_BUFFER];
    uint64_t next_id, accepted, completed, rejected;
    size_t window;
    unsigned passes;
    int stopping, fatal, running, active;
} Server;

typedef struct { int fd; EdgeFramer framer; } Client;
static volatile sig_atomic_t interrupted = 0;

static void on_signal(int signal_number) { (void)signal_number; interrupted = 1; }

static uint64_t now_ns(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        perror("clock_gettime");
        abort();
    }
    return (uint64_t)ts.tv_sec * UINT64_C(1000000000) + (uint64_t)ts.tv_nsec;
}

static const char *policy_name(const Server *s)
{
    return s->queue.policy == EDGE_FIFO ? "fifo" : "priority";
}

/* All state and output operations below run under the same mutex. */
static void fail(Server *s, const char *operation)
{
    if (!s->fatal) fprintf(stderr, "%s: %s\n", operation, strerror(errno));
    s->fatal = 1;
    s->stopping = 1;
    pthread_cond_broadcast(&s->ready);
}

static int write_status(Server *s)
{
    char temporary[PATH_BUFFER], destination[PATH_BUFFER];
    int n = snprintf(temporary, sizeof(temporary), "%s/.status-XXXXXX", s->output);
    int m = snprintf(destination, sizeof(destination), "%s/status.json", s->output);
    if (n < 0 || m < 0 || (size_t)n >= sizeof(temporary) ||
        (size_t)m >= sizeof(destination)) { errno = ENAMETOOLONG; fail(s, "status path"); return 0; }
    int fd = mkstemp(temporary);
    if (fd < 0) { fail(s, "create status"); return 0; }
    FILE *file = fdopen(fd, "w");
    if (!file) { int saved = errno; close(fd); unlink(temporary); errno = saved;
        fail(s, "open status"); return 0; }
    int ok = fprintf(file,
        "{\"policy\":\"%s\",\"queue_depth\":%zu,\"queue_capacity\":%zu,"
        "\"accepted\":%" PRIu64 ",\"completed\":%" PRIu64 ",\"rejected\":%" PRIu64 ","
        "\"running\":%s,\"active\":%s,\"window\":%zu,\"passes\":%u,"
        "\"accepting\":%s,\"fatal\":%s}\n",
        policy_name(s), s->queue.length, s->queue.capacity, s->accepted,
        s->completed, s->rejected, s->running ? "true" : "false",
        s->active ? "true" : "false", s->window, s->passes,
        s->running && !s->stopping ? "true" : "false", s->fatal ? "true" : "false") >= 0;
    if (ok && fflush(file) != 0) ok = 0;
    /* Atomic rename prevents readers observing a partial JSON document. */
    if (fclose(file) != 0) ok = 0;
    if (ok && rename(temporary, destination) != 0) ok = 0;
    if (!ok) { int saved = errno; unlink(temporary); errno = saved;
        fail(s, "write status"); }
    return ok;
}

static int log_task(Server *s, const EdgeTask *task, const char *reason,
                    uint64_t received)
{
    FILE *file = s->csv;
    int ok;
    if (task) {
        ok = fprintf(file, "%" PRIu64 ",%s,%" PRIu64 ",synthetic,%s,%zu,",
             task->id, task->device_id, task->seq,
             task->priority == EDGE_URGENT ? "urgent" : "normal", task->count) >= 0;
    } else ok = fputs(",,,,,,", file) >= 0;
    if (reason) {
        uint64_t finished = now_ns();
        if (fprintf(file, "rejected,%s,%" PRIu64 ",,,%" PRIu64 ",,,%.9f,,\n",
                    reason, received, finished, (double)(finished - received) / 1e6) < 0) ok = 0;
    } else {
        if (fprintf(file, "completed,,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
                    ",%.9f,%.9f,%.9f,%.17g,%.17g\n", task->received_ns,
                    task->enqueued_ns, task->started_ns, task->finished_ns,
                    (double)(task->started_ns - task->enqueued_ns) / 1e6,
                    (double)(task->finished_ns - task->started_ns) / 1e6,
                    (double)(task->finished_ns - task->received_ns) / 1e6,
                    task->rms, task->mean) < 0) ok = 0;
    }
    if (fflush(file) != 0) ok = 0;
    if (!ok) fail(s, "write tasks.csv");
    return ok;
}

static void *worker(void *context)
{
    Server *s = context;
    pthread_mutex_lock(&s->mutex);
    while (!s->fatal) {
        while (!s->queue.length && !s->stopping)
            pthread_cond_wait(&s->ready, &s->mutex);
        if (s->fatal || (!s->queue.length && s->stopping)) break;
        EdgeTask *task = edge_queue_pop(&s->queue);
        s->active = 1;
        /* Snapshot publication is outside the measured compute interval. */
        if (!write_status(s)) { free(task); break; }
        pthread_mutex_unlock(&s->mutex);
        task->started_ns = now_ns();
        edge_compute(task, s->window, s->passes);
        task->finished_ns = now_ns();
        pthread_mutex_lock(&s->mutex);
        s->active = 0;
        if (log_task(s, task, NULL, task->received_ns)) ++s->completed;
        free(task);
        if (!s->fatal) write_status(s);
    }
    s->active = 0;
    pthread_mutex_unlock(&s->mutex);
    return NULL;
}

static int on_frame(const char *data, size_t length, const char *error, void *context)
{
    Server *s = context;
    EdgeTask *task = NULL;
    uint64_t received = now_ns();
    if (!error) {
        task = malloc(sizeof(*task));
        if (!task) {
            pthread_mutex_lock(&s->mutex);
            fail(s, "allocate task");
            pthread_mutex_unlock(&s->mutex);
            return 0;
        }
        if (!edge_parse(data, length, task)) {
            free(task); task = NULL; error = "invalid_message";
        }
    }
    pthread_mutex_lock(&s->mutex);
    if (s->fatal) { free(task); pthread_mutex_unlock(&s->mutex); return 0; }
    if (task) {
        task->id = ++s->next_id;
        task->received_ns = received;
        task->enqueued_ns = now_ns();
        if (edge_queue_push(&s->queue, task)) {
            ++s->accepted;
            pthread_cond_signal(&s->ready);
            task = NULL;
        } else error = "queue_full";
    }
    if (error) {
        if (log_task(s, task, error, received)) ++s->rejected;
        free(task);
    }
    if (!s->fatal) write_status(s);
    int ok = !s->fatal;
    pthread_mutex_unlock(&s->mutex);
    return ok;
}

static int nonblocking(int fd)
{
    int flags = fcntl(fd, F_GETFL, 0);
    return flags >= 0 && fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0;
}

static void close_client(Client *client, Server *s)
{
    if (client->fd >= 0) {
        edge_framer_finish(&client->framer, on_frame, s);
        close(client->fd);
        edge_framer_destroy(&client->framer);
        client->fd = -1;
    }
}

static int receive_loop(Server *s, int listener)
{
    Client clients[MAX_CLIENTS];
    struct pollfd fds[MAX_CLIENTS + 1];
    for (size_t i = 0; i < MAX_CLIENTS; ++i) clients[i].fd = -1;
    int ok = 1;
    while (!interrupted) {
        pthread_mutex_lock(&s->mutex);
        int fatal = s->fatal;
        pthread_mutex_unlock(&s->mutex);
        if (fatal) { ok = 0; break; }
        fds[0].fd = listener;
        fds[0].events = POLLIN;
        for (size_t i = 0; i < MAX_CLIENTS; ++i) {
            fds[i + 1].fd = clients[i].fd;
            fds[i + 1].events = POLLIN;
        }
        int ready = poll(fds, MAX_CLIENTS + 1, 100);
        if (ready < 0) {
            if (errno == EINTR) continue;
            perror("poll"); ok = 0; break;
        }
        if (interrupted) break;
        if (fds[0].revents & (POLLERR | POLLHUP | POLLNVAL)) {
            fprintf(stderr, "listening socket failed\n"); ok = 0; break;
        }
        if (fds[0].revents & POLLIN) {
            /* Bounded accepts per iteration keep existing clients progressing. */
            for (size_t attempt = 0; attempt < MAX_CLIENTS; ++attempt) {
                int fd = accept(listener, NULL, NULL);
                if (fd < 0) {
                    if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) break;
                    perror("accept"); ok = 0; break;
                }
                size_t i = 0;
                while (i < MAX_CLIENTS && clients[i].fd >= 0) ++i;
                if (i == MAX_CLIENTS) { close(fd); continue; }
                if (!nonblocking(fd) || !edge_framer_init(&clients[i].framer)) {
                    close(fd); perror("initialize client"); ok = 0; break;
                }
                clients[i].fd = fd;
            }
        }
        if (!ok) break;
        for (size_t i = 0; i < MAX_CLIENTS; ++i) {
            short events = fds[i + 1].revents;
            if (clients[i].fd < 0 || !events) continue;
            /* Read once per poll cycle to prevent one sender starving others. */
            if (events & (POLLIN | POLLHUP | POLLERR)) {
                char buffer[16384];
                ssize_t n = recv(clients[i].fd, buffer, sizeof(buffer), 0);
                if (n > 0) {
                    if (!edge_framer_feed(&clients[i].framer, buffer, (size_t)n,
                                          on_frame, s)) { ok = 0; break; }
                } else if (n == 0 || (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK))
                    close_client(&clients[i], s);
            } else if (events & POLLNVAL) close_client(&clients[i], s);
        }
        if (!ok) break;
    }
    close(listener);
    pthread_mutex_lock(&s->mutex);
    s->stopping = 1;
    /* Publish closed admission while accepted work is still draining. */
    if (!write_status(s)) ok = 0;
    pthread_cond_broadcast(&s->ready);
    pthread_mutex_unlock(&s->mutex);
    for (size_t i = 0; i < MAX_CLIENTS; ++i) close_client(&clients[i], s);
    return ok;
}

static int unsigned_option(const char *text, unsigned long maximum, unsigned long *value)
{
    char *end;
    if (!*text) return 0;
    for (const char *p = text; *p; ++p) if (*p < '0' || *p > '9') return 0;
    errno = 0;
    *value = strtoul(text, &end, 10);
    return !errno && !*end && *value >= 1 && *value <= maximum;
}

static void usage(FILE *out)
{
    fprintf(out, "Usage: edge_scheduler [--bind IPv4] [--port 9000] "
        "[--policy fifo|priority] [--capacity 32] [--window 8] [--passes 1] "
        "[--output DIR]\n"
        "Limits: capacity 1..4096, window 1..8192, passes 1..1000000.\n"
        "Output directory must not already contain tasks.csv. No network ACK.\n");
}

int main(int argc, char **argv)
{
    Server server;
    memset(&server, 0, sizeof(server));
    const char *bind_address = "127.0.0.1", *output = "output";
    unsigned long port = 9000, capacity = 32, window = 8, passes = 1;
    EdgePolicy policy = EDGE_FIFO;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--help") == 0) { usage(stdout); return 0; }
        if (i + 1 == argc) { usage(stderr); return 2; }
        const char *key = argv[i++], *value = argv[i];
        int valid = 1;
        if (strcmp(key, "--bind") == 0) bind_address = value;
        else if (strcmp(key, "--output") == 0) output = value;
        else if (strcmp(key, "--port") == 0) valid = unsigned_option(value, 65535, &port);
        else if (strcmp(key, "--capacity") == 0) valid = unsigned_option(value, 4096, &capacity);
        else if (strcmp(key, "--window") == 0) valid = unsigned_option(value, EDGE_MAX_SAMPLES, &window);
        else if (strcmp(key, "--passes") == 0) valid = unsigned_option(value, 1000000, &passes);
        else if (strcmp(key, "--policy") == 0) {
            if (strcmp(value, "fifo") == 0) policy = EDGE_FIFO;
            else if (strcmp(value, "priority") == 0) policy = EDGE_PRIORITY;
            else valid = 0;
        } else valid = 0;
        if (!valid) { fprintf(stderr, "Invalid option: %s %s\n", key, value); return 2; }
    }
    if (strlen(output) > sizeof(server.output) - 40 || !*output) {
        fprintf(stderr, "Invalid output path\n"); return 2;
    }
    strcpy(server.output, output);
    server.window = (size_t)window;
    server.passes = (unsigned)passes;
    struct sockaddr_in address;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, bind_address, &address.sin_addr) != 1) {
        fprintf(stderr, "--bind requires an IPv4 address\n"); return 2;
    }
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) { perror("socket"); return 1; }
    int reuse = 1;
    if (setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) != 0 ||
        !nonblocking(listener) || bind(listener, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        listen(listener, MAX_CLIENTS) != 0) {
        perror("listen/bind"); close(listener); return 1;
    }
    if (mkdir(output, 0755) != 0 && errno != EEXIST) {
        perror("create output directory"); close(listener); return 1;
    }
    char csv_path[PATH_BUFFER];
    snprintf(csv_path, sizeof(csv_path), "%s/tasks.csv", output);
    int csv_fd = open(csv_path, O_WRONLY | O_CREAT | O_EXCL, 0644);
    if (csv_fd < 0) { perror("create tasks.csv (use a fresh output directory)"); close(listener); return 1; }
    server.csv = fdopen(csv_fd, "w");
    if (!server.csv) { perror("fdopen CSV"); close(csv_fd); close(listener); return 1; }
    if (!edge_queue_init(&server.queue, (size_t)capacity, policy)) {
        perror("allocate queue"); fclose(server.csv); close(listener); return 1;
    }
    int mutex_error = pthread_mutex_init(&server.mutex, NULL);
    if (mutex_error) { fprintf(stderr, "mutex: %s\n", strerror(mutex_error));
        edge_queue_destroy(&server.queue); fclose(server.csv); close(listener); return 1; }
    int cond_error = pthread_cond_init(&server.ready, NULL);
    if (cond_error) { fprintf(stderr, "condition: %s\n", strerror(cond_error));
        pthread_mutex_destroy(&server.mutex); edge_queue_destroy(&server.queue);
        fclose(server.csv); close(listener); return 1; }
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = on_signal;
    sigemptyset(&action.sa_mask);
    int ok = sigaction(SIGINT, &action, NULL) == 0 && sigaction(SIGTERM, &action, NULL) == 0;
    if (!ok) perror("sigaction");
    if (fputs("task_id,device_id,seq,source,priority,samples,status,reason,received_ns,"
              "enqueued_ns,started_ns,finished_ns,wait_ms,compute_ms,total_ms,rms,mean\n",
              server.csv) < 0 || fflush(server.csv) != 0) { perror("CSV header"); ok = 0; }
    server.running = 1;
    if (ok) ok = write_status(&server);
    pthread_t thread;
    int started = 0;
    if (ok) {
        int error = pthread_create(&thread, NULL, worker, &server);
        if (error) { fprintf(stderr, "pthread_create: %s\n", strerror(error)); ok = 0; }
        else started = 1;
    }
    if (ok) {
        printf("Listening on %s:%lu policy=%s output=%s\n", bind_address, port,
               policy_name(&server), output);
        fflush(stdout);
        ok = receive_loop(&server, listener);
    } else close(listener);
    if (started) {
        int error = pthread_join(thread, NULL);
        if (error) { fprintf(stderr, "pthread_join: %s\n", strerror(error)); return 1; }
    }
    pthread_mutex_lock(&server.mutex);
    server.running = 0;
    server.stopping = 1;
    if (server.fatal) ok = 0;
    if (fflush(server.csv) != 0 || fsync(fileno(server.csv)) != 0) { perror("flush CSV"); ok = 0; }
    if (fclose(server.csv) != 0) { perror("close CSV"); ok = 0; }
    if (!ok) server.fatal = 1;
    if (!write_status(&server)) ok = 0;
    pthread_mutex_unlock(&server.mutex);
    pthread_cond_destroy(&server.ready);
    pthread_mutex_destroy(&server.mutex);
    edge_queue_destroy(&server.queue);
    return ok ? 0 : 1;
}
