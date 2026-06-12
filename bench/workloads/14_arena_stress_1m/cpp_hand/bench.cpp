// 1M cumulative element alloc — C++ with std::vector (heap, free per frame)
#include <cstdio>
#include <cstdint>
#include <vector>

int main(void) {
    int32_t total = 0;
    for (int32_t frame = 0; frame < 100; frame++) {
        std::vector<int32_t> buf;
        buf.reserve(10000);
        for (int32_t j = 0; j < 10000; j++) {
            buf.push_back(j);
        }
        total += buf[5000];
        // vector destructor frees heap
    }
    printf("%d\n", total);
    return 0;
}
