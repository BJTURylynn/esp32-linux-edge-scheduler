#include "EdgeWorkerArduino.h"
#include <cassert>
#include <string>

static uint32_t time_ms = 0;
uint32_t millis() { return time_ms; }
uint32_t micros() { return time_ms * 1000; }
static void tick() { ++time_ms; }
class Capture : public Stream {
public:
    std::string text;
    unsigned flushed = 0;
    size_t write(uint8_t value) override { text += (char)value; return 1; }
    void flush() override { ++flushed; }
};
static void feed(EdgeWorkerRuntime &worker, Capture &output, const std::string &text) {
    for (unsigned char value : text) worker.feed(output, value, tick);
}
static std::string task(unsigned id = 1, const char *session = "0123456789abcdef") {
    return "{\"v\":2,\"type\":\"task\",\"session\":\"" + std::string(session) +
        "\",\"id\":" + std::to_string(id) + ",\"seed\":0,\"count\":16,\"window\":8,\"passes\":2}\n";
}

int main() {
    EdgeWorkerRuntime worker("uno-01");
    Capture output;
    worker.hello(output);
    assert(output.text.find("\"type\":\"hello\"") != std::string::npos);
    output.text.clear();
    feed(worker, output, task()); assert(output.text.empty()); // No welcome.
    feed(worker, output, "{\"v\":2,\"type\":\"welcome\",\"session\":\"0123456789abcdef\"}\n");
    assert(worker.welcomed());
    feed(worker, output, task(1, "1111111111111111")); assert(output.text.empty());
    feed(worker, output, task());
    const size_t accepted = output.text.find("\"type\":\"accept\"");
    const size_t result = output.text.find("\"type\":\"result\"");
    assert(accepted != std::string::npos && result > accepted && result != std::string::npos);
    assert(output.flushed >= 2 && output.text.find("\"compute_us\":4000") != std::string::npos);
    // IDs reflect admission order, not execution order under priority queues.
    output.text.clear(); feed(worker, output, task(16));
    assert(output.text.find("\"type\":\"result\"") != std::string::npos);
    output.text.clear(); feed(worker, output, task(12));
    assert(output.text.find("\"type\":\"result\"") != std::string::npos);
    output.text.clear(); feed(worker, output, task(16));
    assert(output.text.find("duplicate_task") != std::string::npos);
    assert(output.text.find("\"type\":\"result\"") == std::string::npos);
    output.text.clear(); feed(worker, output, task(512));
    assert(output.text.find("\"type\":\"result\"") != std::string::npos);
    output.text.clear(); feed(worker, output, task(513));
    assert(output.text.find("invalid_task") != std::string::npos);
    assert(output.text.find("\"type\":\"accept\"") == std::string::npos);
    assert(output.text.find("\"type\":\"result\"") == std::string::npos);
    output.text.clear();
    feed(worker, output, task());
    assert(output.text.find("duplicate_task") != std::string::npos);
    assert(output.text.find("\"type\":\"result\"") == std::string::npos);
    // Repeating the same welcome cannot permit duplicate execution.
    feed(worker, output, "{\"v\":2,\"type\":\"welcome\",\"session\":\"0123456789abcdef\"}\n");
    output.text.clear(); feed(worker, output, task());
    assert(output.text.find("duplicate_task") != std::string::npos);
    output.text.clear(); feed(worker, output, task(16));
    assert(output.text.find("duplicate_task") != std::string::npos);
    output.text.clear();
    feed(worker, output, std::string(257, 'x') + "\n"); assert(output.text.empty());
    feed(worker, output, "{\"v\":2,\"type\":\"task\",\"session\":\"0123456789abcdef\",\"id\":2,\"seed\":0,\"count\":0,\"window\":8,\"passes\":2}\n");
    assert(output.text.find("invalid_task") != std::string::npos);
    assert(output.text.find("\"type\":\"accept\"") == std::string::npos);
    output.text.clear();
    feed(worker, output, "{\"v\":2,\"type\":\"ping\",\"session\":\"0123456789abcdef\"}\n");
    assert(output.text.find("\"type\":\"pong\"") != std::string::npos);
    // Embedded NUL must invalidate the entire line, not accept its prefix.
    output.text.clear();
    std::string hidden = "{\"v\":2,\"type\":\"probe\"}"; hidden += '\0'; hidden += "junk\n";
    feed(worker, output, hidden); assert(output.text.empty() && worker.welcomed());
    feed(worker, output, "{\"v\":2,\"type\":\"probe\"}\n");
    assert(!worker.welcomed() && output.text.find("\"type\":\"hello\"") != std::string::npos);
    output.text.clear();
    feed(worker, output, "{\"v\":2,\"type\":\"welcome\",\"session\":\"1111111111111111\"}\n");
    feed(worker, output, task(1, "1111111111111111"));
    assert(output.text.find("\"type\":\"result\"") != std::string::npos);
    output.text.clear(); feed(worker, output, task(16, "1111111111111111"));
    assert(output.text.find("\"type\":\"result\"") != std::string::npos);
    output.text.clear(); feed(worker, output, task(512, "1111111111111111"));
    assert(output.text.find("\"type\":\"result\"") != std::string::npos);
    // A genuinely new welcome also resets the bitset, even without probe.
    output.text.clear();
    feed(worker, output, "{\"v\":2,\"type\":\"welcome\",\"session\":\"2222222222222222\"}\n");
    feed(worker, output, task(16, "2222222222222222"));
    assert(output.text.find("\"type\":\"result\"") != std::string::npos);
    // Exact-limit CRLF frame is accepted; extra CR beyond that cap is not.
    output.text.clear();
    std::string ping = "{\"v\":2,\"type\":\"ping\",\"session\":\"2222222222222222\"}";
    ping += std::string(256 - ping.size(), ' ');
    feed(worker, output, ping + "\r\n");
    assert(output.text.find("\"type\":\"pong\"") != std::string::npos);
    output.text.clear(); feed(worker, output, ping + "\r\r\n"); assert(output.text.empty());
    worker.reset(); time_ms += 1001; output.text.clear(); worker.poll(output);
    assert(output.text.find("\"type\":\"hello\"") != std::string::npos);
    return 0;
}
