#ifndef EDGE_COMPUTE_WORKER_ARDUINO_H
#define EDGE_COMPUTE_WORKER_ARDUINO_H

#include <Arduino.h>
#include "EdgeWorker.h"

/* No command queue: process one complete command synchronously, acknowledge,
 * compute on this MCU, then emit the result. The central owns scheduling. */
class EdgeWorkerRuntime {
public:
    explicit EdgeWorkerRuntime(const char *device) : device_(device) { reset(); }

    void reset() {
        length_ = 0; overflow_ = false; session_[0] = '\0';
        memset(seen_, 0, sizeof(seen_)); lastHello_ = 0; started_ = 0;
    }

    void hello(Print &output) {
        output.print(F("{\"v\":2,\"type\":\"hello\",\"device\":\""));
        output.print(device_);
        output.println(F("\"}"));
        lastHello_ = millis();
    }

    void poll(Print &output) {
        if (!session_[0] && millis() - lastHello_ >= 1000UL) hello(output);
        if ((length_ || overflow_) && millis() - started_ > 5000UL) {
            length_ = 0; overflow_ = false;
        }
    }

    bool welcomed() const { return session_[0] != '\0'; }

    void feed(Stream &output, int input, EWTick tick = 0) {
        if (input < 0) return;
        if (!length_ && !overflow_) started_ = millis();
        if (input == '\n') {
            if (length_ && line_[length_ - 1] == '\r') --length_;
            line_[length_] = '\0';
            if (!overflow_) handle(output, tick);
            length_ = 0; overflow_ = false;
        } else if (!overflow_) {
            if ((input < 32 && input != '\t' && input != '\r') || input > 126) {
                overflow_ = true; length_ = 0;
            } else if (length_ < EW_MAX_LINE || (length_ == EW_MAX_LINE && input == '\r')) {
                line_[length_++] = (char)input;
            } else { overflow_ = true; length_ = 0; }
        }
    }

private:
    const char *device_;
    char line_[EW_MAX_LINE + 2];
    char session_[17];
    uint16_t length_;
    bool overflow_;
    uint8_t seen_[EW_MAX_TASK_ID / 8u];
    uint32_t lastHello_, started_;

    void prefix(Print &output, const __FlashStringHelper *type, uint32_t id, bool hasId) {
        output.print(F("{\"v\":2,\"type\":\"")); output.print(type);
        output.print(F("\",\"device\":\"")); output.print(device_);
        output.print(F("\",\"session\":\"")); output.print(session_); output.print('"');
        if (hasId) { output.print(F(",\"id\":")); output.print(id); }
    }

    void error(Stream &output, uint32_t id, const __FlashStringHelper *reason) {
        prefix(output, F("error"), id, true);
        output.print(F(",\"reason\":\"")); output.print(reason); output.println(F("\"}"));
        output.flush();
    }

    void handle(Stream &output, EWTick tick) {
        EWCommand command;
        if (!ew_parse(line_, &command)) {
            // Only correlate a safely parsed task/session/id; malformed or
            // unrelated bytes never execute or invent an acceptance/result.
            if (session_[0] && command.type == EW_TASK && command.id &&
                    strcmp(command.session, session_) == 0)
                error(output, command.id, F("invalid_task"));
            return;
        }
        if (command.type == EW_PROBE) {
            session_[0] = '\0'; memset(seen_, 0, sizeof(seen_));
            hello(output); output.flush();
            return;
        }
        if (command.type == EW_WELCOME) {
            // Repeated WELCOME for the same session must not reset dedup state.
            if (strcmp(command.session, session_) != 0) {
                memcpy(session_, command.session, sizeof(session_));
                memset(seen_, 0, sizeof(seen_));
            }
            return;
        }
        if (!session_[0] || strcmp(command.session, session_) != 0) return;
        if (command.type == EW_PING) {
            prefix(output, F("pong"), 0, false); output.println('}'); output.flush();
            return;
        }
        if (command.type != EW_TASK) return;
        // Priority dispatch deliberately executes IDs out of admission order.
        // A 64-byte session bitset tracks the central's bounded 1..512 IDs.
        const uint16_t bit = (uint16_t)(command.id - 1u);
        const uint8_t mask = (uint8_t)(1u << (bit & 7u));
        if (seen_[bit >> 3u] & mask) { error(output, command.id, F("duplicate_task")); return; }
        seen_[bit >> 3u] |= mask;
        prefix(output, F("accept"), command.id, true); output.println('}');
        output.flush(); // Confirmation must physically leave UART before work.
        const uint32_t started = micros();
        const EWResult result = ew_compute(&command, tick);
        const uint32_t elapsed = micros() - started;
        if (!isfinite(result.mean) || !isfinite(result.rms)) {
            error(output, command.id, F("nonfinite_result")); return;
        }
        prefix(output, F("result"), command.id, true);
        output.print(F(",\"mean\":")); output.print(result.mean, 6);
        output.print(F(",\"rms\":")); output.print(result.rms, 6);
        output.print(F(",\"compute_us\":")); output.print(elapsed);
        output.println('}'); output.flush();
    }
};

#endif
