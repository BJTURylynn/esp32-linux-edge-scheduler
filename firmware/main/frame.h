#ifndef EDGE_FRAME_H
#define EDGE_FRAME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define EDGE_MAX_SAMPLES 8192U
#define EDGE_MAX_DEVICE_ID 32U
#define EDGE_MAX_SEQ UINT64_C(9007199254740991)
#define EDGE_MAX_LINE_BYTES 262144U

bool edge_device_id_valid(const char *device_id);
/* Capacity includes the terminating NUL, which is NOT sent over TCP. */
size_t edge_frame_capacity(size_t samples);
/* Returns the NDJSON length including newline, or 0 on invalid input/no space.
 * Synthetic integers in [-1000,1000] are deterministic for (seq, sample index).
 */
size_t edge_frame_build(char *out, size_t capacity, const char *device_id,
                        uint64_t seq, bool urgent, size_t samples);

#endif
