using System;

class Bench {
    static void Main() {
        const int K = 10000;
        const int N_OUTER = 100;

        string[] strs = new string[K];
        for (int i = 0; i < K; i++) {
            strs[i] = i.ToString();
        }

        int acc = 0;
        for (int outer = 0; outer < N_OUTER; outer++) {
            for (int j = 0; j < K; j++) {
                acc += int.Parse(strs[j]);
            }
        }
        Console.WriteLine(acc);
    }
}
