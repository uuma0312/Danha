using System;

class Bench {
    static void Main() {
        const int N = 50000;
        const int QUERIES = 1000;
        const int GRID_DIM = 64;
        const int TOTAL_CELLS = GRID_DIM * GRID_DIM;
        const int CELL_CAP = 128;
        const double RADIUS = 20.0;

        var px = new double[N];
        var py = new double[N];
        uint state = 2463534242u;
        for (int i = 0; i < N; i++) {
            state ^= state << 13; state ^= state >> 17; state ^= state << 5;
            double sx = (double)(int)((state >> 1) % 6400u) * 0.1;
            state ^= state << 13; state ^= state >> 17; state ^= state << 5;
            double sy = (double)(int)((state >> 1) % 6400u) * 0.1;
            px[i] = sx; py[i] = sy;
        }

        var counts   = new int[TOTAL_CELLS];
        var ent_flat = new int[TOTAL_CELLS * CELL_CAP];

        for (int i = 0; i < N; i++) {
            int cx = (int)(px[i] * 0.1);
            int cy = (int)(py[i] * 0.1);
            if (cx < 0) cx = 0; if (cx >= GRID_DIM) cx = GRID_DIM - 1;
            if (cy < 0) cy = 0; if (cy >= GRID_DIM) cy = GRID_DIM - 1;
            int cidx = cy * GRID_DIM + cx;
            int c_count = counts[cidx];
            if (c_count < CELL_CAP) {
                ent_flat[cidx * CELL_CAP + c_count] = i;
                counts[cidx] = c_count + 1;
            }
        }

        int total_hits = 0;
        double r2 = RADIUS * RADIUS;
        uint qstate = 1234567u;
        for (int q = 0; q < QUERIES; q++) {
            qstate ^= qstate << 13; qstate ^= qstate >> 17; qstate ^= qstate << 5;
            double qx = (double)(int)((qstate >> 1) % 6400u) * 0.1;
            qstate ^= qstate << 13; qstate ^= qstate >> 17; qstate ^= qstate << 5;
            double qy = (double)(int)((qstate >> 1) % 6400u) * 0.1;

            int min_cx = (int)((qx - RADIUS) * 0.1);
            int max_cx = (int)((qx + RADIUS) * 0.1);
            int min_cy = (int)((qy - RADIUS) * 0.1);
            int max_cy = (int)((qy + RADIUS) * 0.1);
            if (min_cx < 0) min_cx = 0; if (max_cx >= GRID_DIM) max_cx = GRID_DIM - 1;
            if (min_cy < 0) min_cy = 0; if (max_cy >= GRID_DIM) max_cy = GRID_DIM - 1;

            for (int cy = min_cy; cy <= max_cy; cy++) {
                for (int cx = min_cx; cx <= max_cx; cx++) {
                    int cidx = cy * GRID_DIM + cx;
                    int cnt = counts[cidx];
                    int base_ = cidx * CELL_CAP;
                    for (int k = 0; k < cnt; k++) {
                        int eid = ent_flat[base_ + k];
                        double dx = px[eid] - qx;
                        double dy = py[eid] - qy;
                        if (dx * dx + dy * dy < r2) {
                            total_hits = total_hits + 1;
                        }
                    }
                }
            }
        }
        Console.WriteLine(total_hits);
    }
}
