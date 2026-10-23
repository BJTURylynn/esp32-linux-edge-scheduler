cmake_minimum_required(VERSION 3.16)
# Build only main and its declared dependencies, keeping the image/tooling small.
set(COMPONENTS main)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(edge_scheduler_device VERSION 0.1.0)
