#include "frame.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

bool edge_device_id_valid(const char *device_id)
{
    if (device_id == NULL || device_id[0] == '\0') {
        return false;
    }
    size_t i = 0;
    for (; device_id[i] != '\0'; ++i) {
        const unsigned char c = (unsigned char)device_id[i];
        if (i >= EDGE_MAX_DEVICE_ID ||
            !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_')) {
            return false;
        }
    }
    return true;
}

size_t edge_frame_capacity(size_t samples)
{
    if (samples == 0 || samples > EDGE_MAX_SAMPLES) {
        return 0;
    }
    /* Each integer + comma is <=6 bytes; 192 covers header, footer and NUL. */
    return samples * 6U + 192U;
}

size_t edge_frame_build(char *out, size_t capacity, const char *device_id,
                        uint64_t seq, bool urgent, size_t samples)
{
    if (out == NULL || capacity == 0 || !edge_device_id_valid(device_id) ||
        seq == 0 || seq > EDGE_MAX_SEQ || edge_frame_capacity(samples) == 0) {
        return 0;
    }
    int n = snprintf(out, capacity,
        "{\"device_id\":\"%s\",\"seq\":%" PRIu64 ",\"priority\":\"%s\","
        "\"source\":\"synthetic\",\"samples\":[",
        device_id, seq, urgent ? "urgent" : "normal");
    if (n < 0 || (size_t)n >= capacity) {
        return 0;
    }
    size_t used = (size_t)n;
    for (size_t i = 0; i < samples; ++i) {
        const int value = (int)((seq % 2001U + i * 37U) % 2001U) - 1000;
        n = snprintf(out + used, capacity - used, "%s%d", i ? "," : "", value);
        if (n < 0 || (size_t)n >= capacity - used) {
            return 0;
        }
        used += (size_t)n;
    }
    if (capacity - used < 4U || used + 3U > EDGE_MAX_LINE_BYTES) {
        return 0;
    }
    memcpy(out + used, "]}\n", 4U);
    return used + 3U;
}
