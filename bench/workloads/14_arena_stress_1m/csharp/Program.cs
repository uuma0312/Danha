using System;
using System.Collections.Generic;

class Bench {
    static void Main() {
        int total = 0;
        for (int frame = 0; frame < 100; frame++) {
            var buf = new List<int>(10000);
            for (int j = 0; j < 10000; j++) {
                buf.Add(j);
            }
            total += buf[5000];
            // GC eventually collects
        }
        Console.WriteLine(total);
    }
}
