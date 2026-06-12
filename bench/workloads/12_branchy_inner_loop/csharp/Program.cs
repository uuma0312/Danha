using System;

class Bench {
    static int Branchy(int n) {
        int acc = 0;
        for (int i = 0; i < n; i++) {
            int x = i % 4;
            if (x == 0) acc += i;
            else if (x == 1) acc += 2 * i;
            else if (x == 2) acc -= i;
            else acc += 3;
        }
        return acc;
    }
    static void Main() {
        Console.WriteLine(Branchy(10000000));
    }
}
