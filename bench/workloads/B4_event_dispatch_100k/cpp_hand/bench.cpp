// B4: 100K events, 8-way virtual/switch dispatch, 8 separate accumulators.
#include <cstdio>
#include <cstdint>
#include <vector>

enum Tag : int32_t { SPAWN, DEATH, DAMAGE, HEAL, MOVE, ITEM, SCORE, TIME };

struct Event {
    int32_t tag;
    int32_t value;
};

int main(void) {
    constexpr int32_t N = 100000;
    std::vector<Event> events;
    events.reserve(N);

    uint32_t state = 2463534242u;
    for (int32_t i = 0; i < N; i++) {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        int32_t tag = (int32_t)(state & 7u);
        int32_t val = (int32_t)((state >> 4) & 1023u);
        if (val == 0) val = 1;
        events.push_back(Event{ tag, val });
    }

    int32_t spawn_acc = 0;
    int32_t death_acc = 0;
    int32_t dmg_acc = 0;
    int32_t heal_acc = 0;
    int32_t move_acc = 0;
    int32_t item_acc = 7;
    int32_t score_acc = 0;
    int32_t time_acc = 0;

    for (int32_t i = 0; i < N; i++) {
        const Event& e = events[(size_t)i];
        int32_t v = e.value;
        switch (e.tag) {
            case SPAWN:  spawn_acc = spawn_acc + 1; break;
            case DEATH:  death_acc = death_acc + 1; break;
            case DAMAGE: dmg_acc = dmg_acc + v; break;
            case HEAL:   heal_acc = heal_acc + v; break;
            case MOVE:   move_acc = move_acc + v; break;
            case ITEM:   item_acc = item_acc ^ v; break;
            case SCORE:  score_acc = score_acc + v * 2; break;
            case TIME:   time_acc = time_acc + v / 3; break;
        }
    }
    int32_t sum = spawn_acc + death_acc + dmg_acc + heal_acc + move_acc + item_acc + score_acc + time_acc;
    printf("%d\n", sum);
    return 0;
}
