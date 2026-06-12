/*
 * danha_win32.c — 단아 언어 Win32 그래픽 백엔드
 *
 * 소프트웨어 렌더러: 픽셀 버퍼를 단아 코드로 채우고 StretchDIBits로 화면 출력.
 * 창 생성, 메시지 펌프, 픽셀 그리기, 사각형, 키 입력을 제공한다.
 *
 * 빌드: gcc -c danha_win32.c -o danha_win32.o -O2
 * 링크: danha compile mygame.dh danha_win32.o
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static HWND      g_hwnd    = NULL;
static HDC       g_hdc     = NULL;
static uint32_t *g_pixels  = NULL;
static int       g_width   = 0;
static int       g_height  = 0;
static int       g_running = 0;
static BITMAPINFO g_bmi;

static LRESULT CALLBACK _wnd_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_CLOSE:
        case WM_DESTROY:
            g_running = 0;
            PostQuitMessage(0);
            return 0;
        case WM_KEYDOWN:
            if (wp == VK_ESCAPE) { g_running = 0; PostQuitMessage(0); }
            return 0;
    }
    return DefWindowProcA(hwnd, msg, wp, lp);
}

/* 창 열기. 성공하면 1, 실패하면 0 반환. */
int gfx_open(const char *title, int width, int height) {
    g_width  = width;
    g_height = height;
    g_pixels = (uint32_t *)calloc((size_t)width * height, sizeof(uint32_t));
    if (!g_pixels) return 0;

    WNDCLASSEXA wc;
    memset(&wc, 0, sizeof(wc));
    wc.cbSize        = sizeof(wc);
    wc.lpfnWndProc   = _wnd_proc;
    wc.hInstance     = GetModuleHandleA(NULL);
    wc.lpszClassName = "DanhaWindow";
    wc.hCursor       = LoadCursorA(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
    RegisterClassExA(&wc);

    RECT r = {0, 0, width, height};
    AdjustWindowRect(&r, WS_OVERLAPPEDWINDOW, FALSE);

    g_hwnd = CreateWindowExA(
        0, "DanhaWindow", title,
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        CW_USEDEFAULT, CW_USEDEFAULT,
        r.right - r.left, r.bottom - r.top,
        NULL, NULL, GetModuleHandleA(NULL), NULL
    );
    if (!g_hwnd) return 0;

    g_hdc = GetDC(g_hwnd);

    memset(&g_bmi, 0, sizeof(g_bmi));
    g_bmi.bmiHeader.biSize        = sizeof(BITMAPINFOHEADER);
    g_bmi.bmiHeader.biWidth       = width;
    g_bmi.bmiHeader.biHeight      = -height; /* 위에서 아래로 */
    g_bmi.bmiHeader.biPlanes      = 1;
    g_bmi.bmiHeader.biBitCount    = 32;
    g_bmi.bmiHeader.biCompression = BI_RGB;

    g_running = 1;
    return 1;
}

/* 이벤트 펌프. 실행 중이면 1, 창이 닫히면 0 반환. */
int gfx_poll(void) {
    MSG msg;
    while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
        if (msg.message == WM_QUIT) { g_running = 0; return 0; }
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
    return g_running;
}

/* 픽셀 버퍼를 화면에 출력 */
void gfx_present(void) {
    StretchDIBits(
        g_hdc,
        0, 0, g_width, g_height,
        0, 0, g_width, g_height,
        g_pixels, &g_bmi, DIB_RGB_COLORS, SRCCOPY
    );
}

/* 전체 화면을 한 색으로 지우기 */
void gfx_clear(int r, int g, int b) {
    uint32_t color = ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b;
    int n = g_width * g_height;
    for (int i = 0; i < n; i++) g_pixels[i] = color;
}

/* 픽셀 하나 그리기 */
void gfx_pixel(int x, int y, int r, int g, int b) {
    if (x < 0 || x >= g_width || y < 0 || y >= g_height) return;
    g_pixels[y * g_width + x] = ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b;
}

/* 채운 사각형 그리기 */
void gfx_rect(int x, int y, int w, int h, int r, int g, int b) {
    uint32_t color = ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b;
    int x1 = x < 0 ? 0 : x;
    int y1 = y < 0 ? 0 : y;
    int x2 = x + w > g_width  ? g_width  : x + w;
    int y2 = y + h > g_height ? g_height : y + h;
    for (int py = y1; py < y2; py++)
        for (int px = x1; px < x2; px++)
            g_pixels[py * g_width + px] = color;
}

/* 선 그리기 (Bresenham) */
void gfx_line(int x0, int y0, int x1, int y1, int r, int g, int b) {
    uint32_t color = ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b;
    int dx = abs(x1 - x0), dy = abs(y1 - y0);
    int sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
    int err = dx - dy;
    for (;;) {
        if (x0 >= 0 && x0 < g_width && y0 >= 0 && y0 < g_height)
            g_pixels[y0 * g_width + x0] = color;
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 > -dy) { err -= dy; x0 += sx; }
        if (e2 <  dx) { err += dx; y0 += sy; }
    }
}

/* 키 눌림 확인. keycode는 Windows VK_ 상수 (숫자키: 48~57, 알파벳: 65~90). */
int gfx_key(int keycode) {
    return (GetAsyncKeyState(keycode) & 0x8000) ? 1 : 0;
}

/* 창 너비/높이 조회 */
int gfx_width(void)  { return g_width; }
int gfx_height(void) { return g_height; }

/* 창 닫기 및 리소스 해제 */
void gfx_close(void) {
    if (g_hdc  && g_hwnd) ReleaseDC(g_hwnd, g_hdc);
    if (g_hwnd) DestroyWindow(g_hwnd);
    free(g_pixels);
    g_hwnd = NULL; g_hdc = NULL; g_pixels = NULL; g_running = 0;
}
