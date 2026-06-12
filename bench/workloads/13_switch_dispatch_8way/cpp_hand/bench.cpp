// 8-way enum (tagged union) dispatch via switch — 50M iter.
#include <cstdio>
#include <cstdint>
#include <vector>

enum class Tag : int32_t { Add, Sub, Mul, Div, Mod, Xor, Shl, Shr };

struct Op {
    Tag tag;
    int32_t v;
};

int main(void) {
    constexpr int32_t K = 1024;
    constexpr int32_t N_OUTER = 50000;

    std::vector<Op> ops;
    ops.reserve(K);

    uint32_t state = 2463534242u;
    for (int32_t i = 0; i < K; i++) {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        int32_t tag = (int32_t)(state & 7u);
        int32_t val = (int32_t)((state >> 4) & 31u);
        if (val == 0) val = 1;
        ops.push_back({(Tag)tag, val});
    }

    int32_t acc = 7;
    for (int32_t outer = 0; outer < N_OUTER; outer++) {
        for (int32_t j = 0; j < K; j++) {
            Op o = ops[(size_t)j];
            switch (o.tag) {
                case Tag::Add: acc = acc + o.v; break;
                case Tag::Sub: acc = acc - o.v; break;
                case Tag::Mul: acc = acc * o.v; break;
                case Tag::Div: acc = acc / o.v; break;
                case Tag::Mod: acc = acc % o.v; break;
                case Tag::Xor: acc = acc ^ o.v; break;
                case Tag::Shl: acc = acc << (o.v & 5); break;
                case Tag::Shr: acc = acc >> (o.v & 5); break;
            }
        }
    }
    printf("%d\n", acc);
    return 0;
}
