#include "frame.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(test) do { if (!(test)) { \
    fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #test); return 1; \
} } while (0)

int main(int argc, char **argv)
{
    const size_t capacity = edge_frame_capacity(EDGE_MAX_SAMPLES);
    char *frame = malloc(capacity + 2U);
    CHECK(frame != NULL);
    CHECK(edge_device_id_valid("esp32-01_A"));
    CHECK(edge_device_id_valid("12345678901234567890123456789012"));
    CHECK(!edge_device_id_valid("123456789012345678901234567890123"));
    CHECK(!edge_device_id_valid("bad\"id"));
    CHECK(!edge_device_id_valid("bad\nid"));
    CHECK(!edge_device_id_valid("bad id"));
    CHECK(!edge_device_id_valid("\xc3\xa9"));
    CHECK(!edge_device_id_valid(""));
    CHECK(!edge_device_id_valid(NULL));
    CHECK(edge_frame_capacity(0) == 0);
    CHECK(edge_frame_capacity(EDGE_MAX_SAMPLES + 1U) == 0);
    CHECK(edge_frame_capacity(SIZE_MAX) == 0);
    CHECK(edge_frame_build(frame, capacity, "esp32-01", 0, false, 1) == 0);
    CHECK(edge_frame_build(frame, capacity, "esp32-01", EDGE_MAX_SEQ + 1U, false, 1) == 0);
    CHECK(edge_frame_build(frame, capacity, "bad\"id", 1, false, 1) == 0);
    CHECK(edge_frame_build(frame, capacity, "esp32-01", 1, false, 0) == 0);
    CHECK(edge_frame_build(NULL, capacity, "esp32-01", 1, false, 1) == 0);
    CHECK(edge_frame_build(frame, 0, "esp32-01", 1, false, 1) == 0);

    const char *expected = "{\"device_id\":\"esp32-01\",\"seq\":1,\"priority\":\"normal\","
        "\"source\":\"synthetic\",\"samples\":[-999,-962,-925]}\n";
    const size_t length = edge_frame_build(frame, capacity, "esp32-01", 1, false, 3);
    CHECK(length == strlen(expected));
    CHECK(strcmp(frame, expected) == 0);
    /* The buffer must include a NUL; every shorter capacity must fail safely. */
    for (size_t small = 1; small <= length; ++small) {
        memset(frame, 'X', capacity + 2U);
        CHECK(edge_frame_build(frame, small, "esp32-01", 1, false, 3) == 0);
        CHECK(frame[small] == 'X' && frame[small + 1U] == 'X');
    }
    CHECK(edge_frame_build(frame, length + 1U, "esp32-01", 1, false, 3) == length);

    frame[capacity] = 'X';
    const size_t maximum = edge_frame_build(frame, capacity,
        "12345678901234567890123456789012", EDGE_MAX_SEQ, true, EDGE_MAX_SAMPLES);
    CHECK(maximum != 0 && maximum <= EDGE_MAX_LINE_BYTES && maximum < capacity);
    CHECK(frame[maximum - 1U] == '\n' && frame[maximum] == '\0');
    CHECK(frame[capacity] == 'X');
    CHECK(strstr(frame, "\"seq\":9007199254740991") != NULL);
    CHECK(strstr(frame, "\"priority\":\"urgent\"") != NULL);
    char *sample = strstr(frame, "\"samples\":[");
    CHECK(sample != NULL);
    sample += strlen("\"samples\":[");
    for (size_t i = 0; i < EDGE_MAX_SAMPLES; ++i) {
        char *end = NULL;
        const long value = strtol(sample, &end, 10);
        CHECK(end != sample && value >= -1000 && value <= 1000);
        if (i + 1U < EDGE_MAX_SAMPLES) {
            CHECK(*end == ',');
            sample = end + 1;
        } else {
            CHECK(strcmp(end, "]}\n") == 0);
        }
    }
    if (argc == 2 && strcmp(argv[1], "--emit-max") == 0) {
        CHECK(fwrite(frame, 1, maximum, stdout) == maximum);
    } else {
        puts("firmware frame boundary tests passed");
    }
    free(frame);
    return 0;
}
