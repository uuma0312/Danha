// 1M virtual dispatch — mixed concrete classes, replay 50 times.
#include <cstdio>
#include <cstdint>
#include <vector>
#include <memory>

struct Shape {
    virtual int32_t compute(int32_t k) const = 0;
    virtual ~Shape() = default;
};

struct Tri  : Shape { int32_t a; explicit Tri(int32_t v):a(v){}
    int32_t compute(int32_t k) const override { return k * 3 + a; } };
struct Quad : Shape { int32_t a; explicit Quad(int32_t v):a(v){}
    int32_t compute(int32_t k) const override { return k * 4 + a; } };
struct Pent : Shape { int32_t a; explicit Pent(int32_t v):a(v){}
    int32_t compute(int32_t k) const override { return k * 5 + a; } };
struct Hex  : Shape { int32_t a; explicit Hex(int32_t v):a(v){}
    int32_t compute(int32_t k) const override { return k * 6 + a; } };

int main(void) {
    constexpr int32_t N = 1000000;
    constexpr int32_t N_OUTER = 50;

    std::vector<std::unique_ptr<Shape>> shapes;
    shapes.reserve(N);

    uint32_t state = 2463534242u;
    for (int32_t i = 0; i < N; i++) {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        int32_t tag = (int32_t)(state & 3u);
        int32_t val = (int32_t)((state >> 4) & 15u);
        switch (tag) {
            case 0: shapes.emplace_back(std::make_unique<Tri>(val));  break;
            case 1: shapes.emplace_back(std::make_unique<Quad>(val)); break;
            case 2: shapes.emplace_back(std::make_unique<Pent>(val)); break;
            case 3: shapes.emplace_back(std::make_unique<Hex>(val));  break;
        }
    }

    int32_t acc = 0;
    for (int32_t outer = 0; outer < N_OUTER; outer++) {
        for (int32_t j = 0; j < N; j++) {
            acc = acc + shapes[(size_t)j]->compute(outer + j);
        }
    }
    printf("%d\n", acc);
    return 0;
}
