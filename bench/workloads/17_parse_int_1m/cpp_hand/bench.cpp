// 1M parse_int via atoi — pre-build 10K numeric strings, parse 100 times.
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <vector>
#include <string>

int main(void) {
    constexpr int32_t K = 10000;
    constexpr int32_t N_OUTER = 100;

    std::vector<std::string> strs;
    strs.reserve(K);
    for (int32_t i = 0; i < K; i++) {
        strs.emplace_back(std::to_string(i));
    }

    int32_t acc = 0;
    for (int32_t outer = 0; outer < N_OUTER; outer++) {
        for (int32_t j = 0; j < K; j++) {
            acc += std::atoi(strs[(size_t)j].c_str());
        }
    }
    printf("%d\n", acc);
    return 0;
}
