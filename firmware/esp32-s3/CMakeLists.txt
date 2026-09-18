# ESP-IDF 工程根配置，以下顺序为 ESP-IDF CMake 所需。
cmake_minimum_required(VERSION 3.22)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# 当前阶段保持最小构建，只编译 main 及其依赖。
idf_build_set_property(MINIMAL_BUILD ON)
project(shuihua_meeting_esp32)
