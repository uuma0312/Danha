using System;
using System.Collections.Generic;

class Bench {
    static void Main() {
        const int ASSETS = 8192;
        const int LOOKUPS = 50000;
        const int ROUNDS = 20;

        var keys = new List<string>(ASSETS);
        var values = new Dictionary<string, int>(16384);

        for (int i = 0; i < ASSETS; i++) {
            string key = "asset_" + i.ToString();
            keys.Add(key);
            values[key] = (i & 1023) + 17;
        }

        uint state = 2463534242u;
        long acc = 0;
        for (int round = 0; round < ROUNDS; round++) {
            for (int j = 0; j < LOOKUPS; j++) {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                int idx = (int)(state & (ASSETS - 1));
                acc += values[keys[idx]];
            }
        }

        Console.WriteLine(acc);
    }
}
