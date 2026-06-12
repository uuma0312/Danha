using System;

class Bench {
    enum Tag { Add, Sub, Mul, Div, Mod, Xor, Shl, Shr }
    struct Op {
        public Tag tag;
        public int v;
        public Op(Tag t, int x) { tag = t; v = x; }
    }

    static void Main() {
        const int K = 1024;
        const int N_OUTER = 50000;

        Op[] ops = new Op[K];

        uint state = 2463534242u;
        for (int i = 0; i < K; i++) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            int tag = (int)(state & 7u);
            int val = (int)((state >> 4) & 31u);
            if (val == 0) val = 1;
            ops[i] = new Op((Tag)tag, val);
        }

        int acc = 7;
        for (int outer = 0; outer < N_OUTER; outer++) {
            for (int j = 0; j < K; j++) {
                Op o = ops[j];
                switch (o.tag) {
                    case Tag.Add: acc = acc + o.v; break;
                    case Tag.Sub: acc = acc - o.v; break;
                    case Tag.Mul: acc = acc * o.v; break;
                    case Tag.Div: acc = acc / o.v; break;
                    case Tag.Mod: acc = acc % o.v; break;
                    case Tag.Xor: acc = acc ^ o.v; break;
                    case Tag.Shl: acc = acc << (o.v & 5); break;
                    case Tag.Shr: acc = acc >> (o.v & 5); break;
                }
            }
        }
        Console.WriteLine(acc);
    }
}
