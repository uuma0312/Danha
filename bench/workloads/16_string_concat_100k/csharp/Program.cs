using System;
using System.Text;

class Bench {
    static void Main() {
        var s = new StringBuilder();
        for (int i = 0; i < 100000; i++) {
            s.Append("abc");
        }
        Console.WriteLine(s.Length);
    }
}
