#include "EdgeWorker.h"
#include <assert.h>
#include <stdio.h>

static unsigned ticks;
static void tick(void) { ++ticks; }

static void check_kernel(uint32_t seed, uint16_t count, uint8_t window, uint16_t passes) {
    EWCommand command;
    EWResult result;
    double total = 0, squares = 0;
    unsigned i;
    memset(&command, 0, sizeof(command));
    command.type = EW_TASK; command.id = 1; command.seed = seed;
    command.count = count; command.window = window; command.passes = passes;
    assert(ew_parameters(&command));
    for (i = 0; i < count; ++i) {
        unsigned start = i + 1 > window ? i + 1 - window : 0;
        unsigned j;
        double sum = 0, average;
        for (j = start; j <= i; ++j) sum += (double)ew_sample((uint16_t)j, seed);
        average = sum / (i + 1 - start);
        total += average; squares += average * average;
    }
    ticks = 0;
    result = ew_compute(&command, tick);
    assert(ticks == (unsigned)passes * ((count + 31u) / 32u + 1u));
    assert(fabs((double)result.mean - total / count) <= 1e-4 + 1e-4 * fabs(total / count));
    assert(fabs((double)result.rms - sqrt(squares / count)) <= 1e-4 + 1e-4 * sqrt(squares / count));
}

static void good(const char *line, EWType type) {
    EWCommand command;
    assert(ew_parse(line, &command));
    assert(command.type == type);
}

static void bad(const char *line) {
    EWCommand command;
    assert(!ew_parse(line, &command));
}

int main(void) {
    const char *task = "{\"v\":2,\"type\":\"task\",\"session\":\"0123456789abcdef\",\"id\":1,\"seed\":4294967295,\"count\":256,\"window\":16,\"passes\":1000}";
    EWCommand command;
    char too_long[EW_MAX_LINE + 2];
    good(task, EW_TASK);
    assert(ew_parse(task, &command) && command.seed == UINT32_MAX && command.passes == 1000);
    good("{\"v\":2,\"type\":\"task\",\"session\":\"0123456789abcdef\",\"id\":512,\"seed\":0,\"count\":1,\"window\":1,\"passes\":1}", EW_TASK);
    bad("{\"v\":2,\"type\":\"task\",\"session\":\"0123456789abcdef\",\"id\":513,\"seed\":0,\"count\":1,\"window\":1,\"passes\":1}");
    good(" { \"type\":\"welcome\", \"session\":\"0123456789abcdef\", \"v\":2 } \r", EW_WELCOME);
    good("{\"v\":2,\"type\":\"probe\"}", EW_PROBE);
    good("{\"v\":2,\"type\":\"ping\",\"session\":\"0123456789abcdef\"}", EW_PING);
    bad(""); bad("{}"); bad("[]");
    bad("{\"v\":2,\"type\":\"probe\"");
    bad("{\"v\":2,\"type\":\"probe\",}");
    bad("{\"v\":2,\"type\":\"probe\"}x");
    bad("{\"v\":2,\"v\":2,\"type\":\"probe\"}");
    bad("{\"v\":2,\"type\":\"probe\",\"extra\":0}");
    bad("{\"v\":02,\"type\":\"probe\"}");
    bad("{\"v\":2.0,\"type\":\"probe\"}");
    bad("{\"v\":2e0,\"type\":\"probe\"}");
    bad("{\"v\":-2,\"type\":\"probe\"}");
    bad("{\"v\":4294967296,\"type\":\"probe\"}");
    bad("{\"v\":3,\"type\":\"probe\"}");
    bad("{\"v\":2,\"type\":\"welcome\",\"session\":\"0123456789abcdeF\"}");
    bad("{\"v\":2,\"type\":\"ping\",\"session\":\"0123456789abcdef\",\"id\":1}");
    bad("{\"v\":2,\"type\":\"task\",\"session\":\"0123456789abcdef\",\"id\":0,\"seed\":0,\"count\":256,\"window\":16,\"passes\":1}");
    bad("{\"v\":2,\"type\":\"task\",\"session\":\"0123456789abcdef\",\"id\":1,\"seed\":0,\"count\":257,\"window\":16,\"passes\":1}");
    bad("{\"v\":2,\"type\":\"task\",\"session\":\"0123456789abcdef\",\"id\":1,\"seed\":0,\"count\":1,\"window\":0,\"passes\":1}");
    bad("{\"v\":2,\"type\":\"task\",\"session\":\"0123456789abcdef\",\"id\":1,\"seed\":0,\"count\":1,\"window\":16,\"passes\":1001}");
    memset(too_long, ' ', sizeof(too_long) - 1); too_long[sizeof(too_long) - 1] = '\0'; bad(too_long);
    check_kernel(0, 1, 1, 1);
    check_kernel(100, 16, 8, 2);
    check_kernel(200, 256, 16, 1000);
    check_kernel(UINT32_MAX, 256, 1, 3);
    check_kernel(UINT32_MAX, 2, 16, 1);
    /* Include phase wrap, non-power-of-two reciprocal windows, all bounds,
     * and the maximum repeated workload with uint32 seeds. */
    {
        const uint32_t seeds[] = {0, 100, 200, 201, UINT32_MAX};
        const uint16_t counts[] = {1, 2, 16, 255, 256};
        const uint8_t windows[] = {1, 3, 7, 8, 15, 16};
        unsigned s, c, w;
        for (s = 0; s < sizeof(seeds) / sizeof(seeds[0]); ++s)
            for (c = 0; c < sizeof(counts) / sizeof(counts[0]); ++c)
                for (w = 0; w < sizeof(windows) / sizeof(windows[0]); ++w)
                    check_kernel(seeds[s], counts[c], windows[w], 1);
    }
    check_kernel(UINT32_MAX, 1, 16, 1000);
    check_kernel(UINT32_MAX, 256, 16, 1000);
    check_kernel(0, 256, 1, 1000);
    puts("compute worker parser and streaming float kernel passed");
    return 0;
}
