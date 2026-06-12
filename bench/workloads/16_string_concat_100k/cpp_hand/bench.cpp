// 100K string concatenations via std::string (heap-backed, geometric growth).
#include <cstdio>
#include <string>

int main(void) {
    std::string s;
    for (int i = 0; i < 100000; i++) {
        s += "abc";
    }
    printf("%zu\n", s.size());
    return 0;
}
