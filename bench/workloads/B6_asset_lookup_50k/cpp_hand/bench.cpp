// B6: Asset/prefab-style lookup pressure using string-key hash table.
#include <cstdint>
#include <cstdio>
#include <string>
#include <unordered_map>
#include <vector>

int main(void) {
    constexpr int32_t ASSETS = 8192;
    constexpr int32_t LOOKUPS = 50000;
    constexpr int32_t ROUNDS = 20;

    std::vector<std::string> keys;
    keys.reserve(ASSETS);
    std::unordered_map<std::string, int32_t> values;
    values.reserve(16384);

    for (int32_t i = 0; i < ASSETS; i++) {
        std::string key = "asset_" + std::to_string(i);
        keys.push_back(key);
        values.emplace(keys.back(), (i & 1023) + 17);
    }

    uint32_t state = 2463534242u;
    int64_t acc = 0;
    for (int32_t round = 0; round < ROUNDS; round++) {
        for (int32_t j = 0; j < LOOKUPS; j++) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            int32_t idx = (int32_t)(state & (ASSETS - 1));
            acc += values[keys[(size_t)idx]];
        }
    }

    std::printf("%lld\n", (long long)acc);
    return 0;
}
