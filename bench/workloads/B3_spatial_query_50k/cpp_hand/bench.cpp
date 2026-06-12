// B3: spatial hash grid radius query — 50K entities, 1K queries, 64x64 cells.
// Flat counts[] + ent_flat[] to match Danha layout.
#include <cstdio>
#include <cstdint>
#include <cstdlib>

int main(void) {
    constexpr int32_t N = 50000;
    constexpr int32_t QUERIES = 1000;
    constexpr int32_t GRID_DIM = 64;
    constexpr int32_t TOTAL_CELLS = GRID_DIM * GRID_DIM;
    constexpr int32_t CELL_CAP = 128;
    constexpr double RADIUS = 20.0;

    double* px = (double*)malloc(sizeof(double) * N);
    double* py = (double*)malloc(sizeof(double) * N);
    uint32_t state = 2463534242u;
    for (int32_t i = 0; i < N; i++) {
        state ^= state << 13; state ^= state >> 17; state ^= state << 5;
        double sx = (double)(int32_t)((state >> 1) % 6400u) * 0.1;
        state ^= state << 13; state ^= state >> 17; state ^= state << 5;
        double sy = (double)(int32_t)((state >> 1) % 6400u) * 0.1;
        px[i] = sx; py[i] = sy;
    }

    int32_t* counts   = (int32_t*)calloc(TOTAL_CELLS, sizeof(int32_t));
    int32_t* ent_flat = (int32_t*)calloc((size_t)TOTAL_CELLS * CELL_CAP, sizeof(int32_t));

    for (int32_t i = 0; i < N; i++) {
        int32_t cx = (int32_t)(px[i] * 0.1);
        int32_t cy = (int32_t)(py[i] * 0.1);
        if (cx < 0) cx = 0; if (cx >= GRID_DIM) cx = GRID_DIM - 1;
        if (cy < 0) cy = 0; if (cy >= GRID_DIM) cy = GRID_DIM - 1;
        int32_t cidx = cy * GRID_DIM + cx;
        int32_t c_count = counts[cidx];
        if (c_count < CELL_CAP) {
            ent_flat[cidx * CELL_CAP + c_count] = i;
            counts[cidx] = c_count + 1;
        }
    }

    int32_t total_hits = 0;
    double r2 = RADIUS * RADIUS;
    uint32_t qstate = 1234567u;
    for (int32_t q = 0; q < QUERIES; q++) {
        qstate ^= qstate << 13; qstate ^= qstate >> 17; qstate ^= qstate << 5;
        double qx = (double)(int32_t)((qstate >> 1) % 6400u) * 0.1;
        qstate ^= qstate << 13; qstate ^= qstate >> 17; qstate ^= qstate << 5;
        double qy = (double)(int32_t)((qstate >> 1) % 6400u) * 0.1;

        int32_t min_cx = (int32_t)((qx - RADIUS) * 0.1);
        int32_t max_cx = (int32_t)((qx + RADIUS) * 0.1);
        int32_t min_cy = (int32_t)((qy - RADIUS) * 0.1);
        int32_t max_cy = (int32_t)((qy + RADIUS) * 0.1);
        if (min_cx < 0) min_cx = 0; if (max_cx >= GRID_DIM) max_cx = GRID_DIM - 1;
        if (min_cy < 0) min_cy = 0; if (max_cy >= GRID_DIM) max_cy = GRID_DIM - 1;

        for (int32_t cy = min_cy; cy <= max_cy; cy++) {
            for (int32_t cx = min_cx; cx <= max_cx; cx++) {
                int32_t cidx = cy * GRID_DIM + cx;
                int32_t cnt = counts[cidx];
                int32_t base = cidx * CELL_CAP;
                for (int32_t k = 0; k < cnt; k++) {
                    int32_t eid = ent_flat[base + k];
                    double dx = px[eid] - qx;
                    double dy = py[eid] - qy;
                    if (dx * dx + dy * dy < r2) {
                        total_hits = total_hits + 1;
                    }
                }
            }
        }
    }
    printf("%d\n", total_hits);
    free(px); free(py); free(counts); free(ent_flat);
    return 0;
}
