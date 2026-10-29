#ifndef EDGE_COMPUTE_WORKER_H
#define EDGE_COMPUTE_WORKER_H

/* C and C++ portable. No Arduino dependency, allocation or sample array. */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <math.h>

#define EW_MAX_LINE 256u
#define EW_MAX_COUNT 256u
#define EW_MAX_WINDOW 16u
#define EW_MAX_PASSES 1000u
#define EW_MAX_TASK_ID 512u

typedef enum { EW_INVALID = 0, EW_WELCOME, EW_TASK, EW_PING, EW_PROBE } EWType;
typedef struct {
    EWType type;
    char session[17];
    uint32_t id, seed;
    uint16_t count, passes;
    uint8_t window;
} EWCommand;
typedef struct { float mean, rms; } EWResult;
typedef void (*EWTick)(void);

static inline int ew_parameters(const EWCommand *command) {
    return command->id >= 1 && command->id <= EW_MAX_TASK_ID &&
        command->count >= 1 && command->count <= EW_MAX_COUNT &&
        command->window >= 1 && command->window <= EW_MAX_WINDOW &&
        command->passes >= 1 && command->passes <= EW_MAX_PASSES;
}

static inline int16_t ew_sample(uint16_t index, uint32_t seed) {
    return (int16_t)((index + seed % UINT32_C(201)) % UINT32_C(201)) - 100;
}

static inline EWResult ew_compute(const EWCommand *command, EWTick tick) {
    int16_t ring[EW_MAX_WINDOW];
    EWResult result = {0.0f, 0.0f};
    const uint8_t window = command->window;
    const uint16_t count = command->count;
    const uint16_t initial_phase = (uint16_t)(command->seed % UINT32_C(201));
    const float full_window_reciprocal = 1.0f / (float)window;
    uint16_t pass;
    for (pass = 0; pass < command->passes; ++pass) {
        int16_t sum = 0;
        uint8_t used = 0, cursor = 0;
        uint16_t phase = initial_phase;
        uint16_t index;
        /* Volatile accumulation makes every repetition observable to the
         * compiler: it may not hoist a pure identical pass out of the loop.
         * All passes still perform the actual floating-point calculation. */
        volatile float total = 0.0f, squares = 0.0f;
        for (index = 0; index < count; ++index) {
            int16_t sample = (int16_t)phase - 100;
            float average;
            /* The exact same synthetic sequence without AVR 32-bit remainder
             * on every sample. Every pass still starts at the same seed. */
            if (++phase == 201u) phase = 0;
            if (used == window) sum = (int16_t)(sum - ring[cursor]);
            else ++used;
            ring[cursor] = sample;
            sum = (int16_t)(sum + sample);
            if (++cursor == window) cursor = 0;
            /* Warm-up uses its actual denominator. Once full, a constant
             * reciprocal avoids repeated software float division on Uno. */
            average = used == window ? (float)sum * full_window_reciprocal
                                     : (float)sum / (float)used;
            total = total + average;
            squares = squares + average * average;
            if (tick && !(index & 31u)) tick();
        }
        result.mean = total / (float)count;
        result.rms = sqrtf(squares / (float)count);
        if (tick) tick();
    }
    return result;
}

static inline void ew_space(const char **p) {
    while (**p == ' ' || **p == '\t' || **p == '\r' || **p == '\n') ++*p;
}

static inline int ew_string(const char **p, const char **start, size_t *length) {
    const char *at;
    if (**p != '"') return 0;
    at = ++*p;
    while (**p && **p != '"') {
        unsigned char c = (unsigned char)**p;
        /* The v2 flat protocol has no escaped strings or non-ASCII fields. */
        if (c < 32 || c > 126 || c == '\\') return 0;
        ++*p;
    }
    if (**p != '"') return 0;
    *start = at;
    *length = (size_t)(*p - at);
    ++*p;
    return 1;
}

static inline int ew_equal(const char *start, size_t length, const char *value) {
    return strlen(value) == length && memcmp(start, value, length) == 0;
}

static inline int ew_uint(const char **p, uint32_t *value) {
    uint32_t number = 0;
    int leading_zero;
    if (**p < '0' || **p > '9') return 0;
    leading_zero = **p == '0';
    do {
        uint8_t digit = (uint8_t)(**p - '0');
        if (number > (UINT32_MAX - digit) / 10u) return 0;
        number = number * 10u + digit;
        ++*p;
        if (leading_zero && **p >= '0' && **p <= '9') return 0;
    } while (**p >= '0' && **p <= '9');
    *value = number;
    return 1;
}

static inline int ew_parse(const char *line, EWCommand *command) {
    const char *p = line, *start;
    size_t length;
    uint16_t fields = 0;
    uint32_t version = 0;
    int closed = 0;
    memset(command, 0, sizeof(*command));
    if (strlen(line) > EW_MAX_LINE) return 0;
    ew_space(&p);
    if (*p++ != '{') return 0;
    ew_space(&p);
    if (*p == '}') return 0;
    while (*p) {
        unsigned key;
        uint16_t bit;
        uint32_t number;
        if (!ew_string(&p, &start, &length)) return 0;
        if (ew_equal(start, length, "v")) key = 0;
        else if (ew_equal(start, length, "type")) key = 1;
        else if (ew_equal(start, length, "session")) key = 2;
        else if (ew_equal(start, length, "id")) key = 3;
        else if (ew_equal(start, length, "seed")) key = 4;
        else if (ew_equal(start, length, "count")) key = 5;
        else if (ew_equal(start, length, "window")) key = 6;
        else if (ew_equal(start, length, "passes")) key = 7;
        else return 0;
        bit = (uint16_t)(1u << key);
        if (fields & bit) return 0;
        fields |= bit;
        ew_space(&p);
        if (*p++ != ':') return 0;
        ew_space(&p);
        if (key == 1 || key == 2) {
            size_t i;
            if (!ew_string(&p, &start, &length)) return 0;
            if (key == 1) {
                if (ew_equal(start, length, "welcome")) command->type = EW_WELCOME;
                else if (ew_equal(start, length, "task")) command->type = EW_TASK;
                else if (ew_equal(start, length, "ping")) command->type = EW_PING;
                else if (ew_equal(start, length, "probe")) command->type = EW_PROBE;
                else return 0;
            } else {
                if (length != 16) return 0;
                for (i = 0; i < length; ++i)
                    if (!((start[i] >= '0' && start[i] <= '9') ||
                          (start[i] >= 'a' && start[i] <= 'f'))) return 0;
                memcpy(command->session, start, 16);
                command->session[16] = '\0';
            }
        } else {
            if (!ew_uint(&p, &number)) return 0;
            switch (key) {
            case 0: version = number; break;
            case 3: command->id = number; break;
            case 4: command->seed = number; break;
            case 5: if (number > EW_MAX_COUNT) return 0; command->count = (uint16_t)number; break;
            case 6: if (number > EW_MAX_WINDOW) return 0; command->window = (uint8_t)number; break;
            case 7: if (number > EW_MAX_PASSES) return 0; command->passes = (uint16_t)number; break;
            default: return 0;
            }
        }
        ew_space(&p);
        if (*p == '}') { ++p; closed = 1; break; }
        if (*p++ != ',') return 0;
        ew_space(&p);
    }
    ew_space(&p);
    if (!closed || *p || version != 2) return 0;
    if (command->type == EW_TASK) return fields == 255u && ew_parameters(command);
    if (command->type == EW_PROBE) return fields == 3u;
    if (command->type == EW_WELCOME || command->type == EW_PING) return fields == 7u;
    return 0;
}

#endif
