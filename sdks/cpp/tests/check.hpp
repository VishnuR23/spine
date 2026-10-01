// Shared assertion helpers for the C++ SDK tests. No framework, on purpose:
// see the comment at the top of test_client.cpp.

#ifndef SPINE_TESTS_CHECK_HPP
#define SPINE_TESTS_CHECK_HPP

#include <cstdio>
#include <string>

namespace spine_test {

inline int g_failures = 0;
inline int g_checks = 0;

inline void check(bool condition, const char* what) {
    ++g_checks;
    if (!condition) {
        ++g_failures;
        std::printf("  FAIL  %s\n", what);
    }
}

inline void check_eq(const std::string& actual, const std::string& expected,
                     const char* what) {
    ++g_checks;
    if (actual != expected) {
        ++g_failures;
        std::printf("  FAIL  %s\n        expected: %s\n        actual:   %s\n",
                    what, expected.c_str(), actual.c_str());
    }
}

}  // namespace spine_test

void run_client_tests();
void run_transport_tests();
void run_lifecycle_tests();

#endif  // SPINE_TESTS_CHECK_HPP
