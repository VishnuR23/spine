cmake_minimum_required(VERSION 3.16)
project(spine_cpp LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(SPINE_BUILD_TESTS "Build the test binary" ON)
option(SPINE_BUILD_EXAMPLES "Build the example binaries" ON)

find_package(CURL REQUIRED)

add_library(spine_client
    src/client.cpp
    src/finance.cpp
)

target_include_directories(spine_client PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>
)

target_link_libraries(spine_client PUBLIC CURL::libcurl)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(spine_client PRIVATE -Wall -Wextra -Wpedantic)
endif()

if(SPINE_BUILD_TESTS)
    enable_testing()
    add_executable(spine_tests tests/test_client.cpp)
    target_link_libraries(spine_tests PRIVATE spine_client)
    add_test(NAME spine_tests COMMAND spine_tests)
endif()

if(SPINE_BUILD_EXAMPLES)
    add_executable(pretrade_gate examples/pretrade_gate.cpp)
    target_link_libraries(pretrade_gate PRIVATE spine_client)
endif()

include(GNUInstallDirs)
install(TARGETS spine_client ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
