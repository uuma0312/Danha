using System;

class Bench {
    static void Main() {
        const int N = 4194304;
        const int ITERS = 4194304;
        int[] a = new int[N];

        uint state = 2463534242u;
        for (int i = 0; i < ITERS; i++) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            uint idx = state & (uint)(N - 1);
            a[idx] += 1;
        }

        int sum = 0;
        for (int i = 0; i < N; i += 1024) {
            sum += a[i];
        }
        Console.WriteLine(sum);
    }
}
