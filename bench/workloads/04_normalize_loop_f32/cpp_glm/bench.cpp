#include <cstdio>
#include <cstdint>
#include <glm/glm.hpp>

int main(void) {
    int32_t n = 10000000;
    double acc = 0.0;
    for (int32_t i = 0; i < n; i++) {
        float x = (float)i;
        glm::vec4 v(x, x + 1.0f, x + 2.0f, x + 3.0f);
        glm::vec4 u = glm::normalize(v);
        acc += (double)u.x + (double)u.y + (double)u.z + (double)u.w;
    }
    printf("%g\n", acc);
    return 0;
}
