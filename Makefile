.PHONY: all test sanitize experiment
all:
	$(MAKE) -C linux all

test: all
	$(MAKE) -C linux test
	cmake -S firmware/tests -B build/firmware-host
	cmake --build build/firmware-host
	ctest --test-dir build/firmware-host --output-on-failure
	EDGE_SERVER="$(CURDIR)/linux/build/edge_scheduler" EDGE_FRAME_TEST="$(CURDIR)/build/firmware-host/test_frame" python3 -m unittest discover -s tests -v

sanitize:
	$(MAKE) -C linux sanitize

experiment: all
	python3 -m tools.replay generate results/workload.jsonl
	python3 -m tools.experiment --workload results/workload.jsonl --output results/comparison
