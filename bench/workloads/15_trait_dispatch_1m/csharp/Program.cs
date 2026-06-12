using System;

abstract class Shape {
    public abstract int Compute(int k);
}

sealed class Tri  : Shape { int a; public Tri(int v){a=v;}  public override int Compute(int k){return k*3+a;} }
sealed class Quad : Shape { int a; public Quad(int v){a=v;} public override int Compute(int k){return k*4+a;} }
sealed class Pent : Shape { int a; public Pent(int v){a=v;} public override int Compute(int k){return k*5+a;} }
sealed class Hex  : Shape { int a; public Hex(int v){a=v;}  public override int Compute(int k){return k*6+a;} }

class Bench {
    static void Main() {
        const int N = 1000000;
        const int N_OUTER = 50;

        Shape[] shapes = new Shape[N];

        uint state = 2463534242u;
        for (int i = 0; i < N; i++) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            int tag = (int)(state & 3u);
            int val = (int)((state >> 4) & 15u);
            switch (tag) {
                case 0: shapes[i] = new Tri(val);  break;
                case 1: shapes[i] = new Quad(val); break;
                case 2: shapes[i] = new Pent(val); break;
                default: shapes[i] = new Hex(val); break;
            }
        }

        int acc = 0;
        for (int outer = 0; outer < N_OUTER; outer++) {
            for (int j = 0; j < N; j++) {
                acc = acc + shapes[j].Compute(outer + j);
            }
        }
        Console.WriteLine(acc);
    }
}
