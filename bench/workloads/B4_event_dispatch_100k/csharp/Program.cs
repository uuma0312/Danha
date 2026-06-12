using System;
using System.Collections.Generic;

class Bench {
    enum Tag { Spawn, Death, Damage, Heal, Move, Item, Score, Time }
    struct Event { public int tag; public int value; }

    static void Main() {
        const int N = 100000;
        var events = new List<Event>(N);
        uint state = 2463534242u;
        for (int i = 0; i < N; i++) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            int tag = (int)(state & 7u);
            int val = (int)((state >> 4) & 1023u);
            if (val == 0) val = 1;
            events.Add(new Event { tag = tag, value = val });
        }

        int spawn_acc = 0;
        int death_acc = 0;
        int dmg_acc = 0;
        int heal_acc = 0;
        int move_acc = 0;
        int item_acc = 7;
        int score_acc = 0;
        int time_acc = 0;

        for (int i = 0; i < N; i++) {
            var e = events[i];
            int v = e.value;
            switch ((Tag)e.tag) {
                case Tag.Spawn:  spawn_acc = spawn_acc + 1; break;
                case Tag.Death:  death_acc = death_acc + 1; break;
                case Tag.Damage: dmg_acc = dmg_acc + v; break;
                case Tag.Heal:   heal_acc = heal_acc + v; break;
                case Tag.Move:   move_acc = move_acc + v; break;
                case Tag.Item:   item_acc = item_acc ^ v; break;
                case Tag.Score:  score_acc = score_acc + v * 2; break;
                case Tag.Time:   time_acc = time_acc + v / 3; break;
            }
        }
        int sum = spawn_acc + death_acc + dmg_acc + heal_acc + move_acc + item_acc + score_acc + time_acc;
        Console.WriteLine(sum);
    }
}
