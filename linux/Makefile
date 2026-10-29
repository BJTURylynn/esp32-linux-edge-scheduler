CC ?= cc
CPPFLAGS += -Iinclude -D_POSIX_C_SOURCE=200809L
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Werror -pedantic -pthread
LDLIBS += -lm -pthread
BUILD ?= build

.PHONY: all test integration sanitize clean
all: $(BUILD)/edge_scheduler $(BUILD)/edge_dispatcher

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/core.o: src/core.c include/edge.h | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(BUILD)/main.o: src/main.c include/edge.h | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(BUILD)/edge_scheduler: $(BUILD)/main.o $(BUILD)/core.o
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

$(BUILD)/edge_dispatcher: src/dispatch.c | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) $< $(LDLIBS) -o $@

$(BUILD)/test_core: tests/test_core.c $(BUILD)/core.o include/edge.h
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) tests/test_core.c $(BUILD)/core.o $(LDLIBS) -o $@

test: $(BUILD)/test_core
	./$(BUILD)/test_core

integration: $(BUILD)/edge_scheduler
	EDGE_SERVER="$(abspath $(BUILD)/edge_scheduler)" python3 ../tests/test_lifecycle.py -v

sanitize:
	$(MAKE) BUILD=build-sanitize CFLAGS='-O1 -g -std=c11 -Wall -Wextra -Werror -pedantic -pthread -fno-omit-frame-pointer -fsanitize=address,undefined' all test

clean:
	rm -rf build build-sanitize
