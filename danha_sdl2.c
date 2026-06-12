/*
 * danha_sdl2.c — 단아 언어 SDL2 그래픽 백엔드
 *
 * 소프트웨어 렌더러: SDL_Surface에 픽셀을 채우고 SDL_UpdateWindowSurface로 출력.
 * 창 관리, 이벤트 펌프, 픽셀/사각형/선 그리기, 키보드/마우스 입력, 타이머 제공.
 *
 * 빌드 (Linux/Mac):
 *   gcc -c danha_sdl2.c -o danha_sdl2.o $(sdl2-config --cflags) -O2
 *   gcc game.o danha_sdl2.o $(sdl2-config --libs) -o game
 *
 * 빌드 (Windows MinGW):
 *   gcc -c danha_sdl2.c -o danha_sdl2.o -I<SDL2_include_path> -O2
 *   gcc game.o danha_sdl2.o -L<SDL2_lib_path> -lSDL2 -o game
 */

#include <SDL2/SDL.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static SDL_Window   *g_window   = NULL;
static SDL_Surface  *g_surface  = NULL;
static SDL_Surface  *g_screen   = NULL;  /* 창 surface (SDL_GetWindowSurface) */
static int           g_width    = 0;
static int           g_height   = 0;
static int           g_running  = 0;
static const uint8_t *g_keys    = NULL;  /* SDL_GetKeyboardState 결과 */
static int           g_mouse_x  = 0;
static int           g_mouse_y  = 0;
static uint32_t      g_mouse_btn = 0;

/* ---- 내부 헬퍼 ---- */
static uint32_t _rgb(int r, int g, int b) {
    return SDL_MapRGB(g_surface->format,
                      (uint8_t)r, (uint8_t)g, (uint8_t)b);
}

/* ---- 창 관리 ---- */

/* 창 열기. 성공 1, 실패 0. */
int sdl_open(const char *title, int width, int height) {
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS) < 0) return 0;

    g_window = SDL_CreateWindow(
        title,
        SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
        width, height,
        SDL_WINDOW_SHOWN
    );
    if (!g_window) { SDL_Quit(); return 0; }

    g_surface = SDL_CreateRGBSurface(0, width, height, 32,
                                     0x00FF0000, 0x0000FF00,
                                     0x000000FF, 0xFF000000);
    if (!g_surface) { SDL_DestroyWindow(g_window); SDL_Quit(); return 0; }

    g_screen  = SDL_GetWindowSurface(g_window);
    g_width   = width;
    g_height  = height;
    g_running = 1;
    g_keys    = SDL_GetKeyboardState(NULL);
    return 1;
}

/* 이벤트 펌프. 창이 열려 있으면 1, 닫혔으면 0. */
int sdl_poll(void) {
    SDL_Event ev;
    while (SDL_PollEvent(&ev)) {
        if (ev.type == SDL_QUIT) { g_running = 0; }
        if (ev.type == SDL_KEYDOWN && ev.key.keysym.sym == SDLK_ESCAPE) {
            g_running = 0;
        }
    }
    SDL_PumpEvents();
    g_keys = SDL_GetKeyboardState(NULL);
    g_mouse_btn = SDL_GetMouseState(&g_mouse_x, &g_mouse_y);
    return g_running;
}

/* 현재 프레임을 화면에 출력. */
void sdl_present(void) {
    SDL_BlitSurface(g_surface, NULL, g_screen, NULL);
    SDL_UpdateWindowSurface(g_window);
}

/* 창과 SDL 자원 해제. */
void sdl_close(void) {
    if (g_surface) { SDL_FreeSurface(g_surface); g_surface = NULL; }
    if (g_window)  { SDL_DestroyWindow(g_window); g_window = NULL; }
    SDL_Quit();
    g_running = 0;
}

int sdl_width(void)  { return g_width;  }
int sdl_height(void) { return g_height; }

/* ---- 그리기 ---- */

/* 화면 전체를 단색으로 채우기. */
void sdl_clear(int r, int g, int b) {
    if (!g_surface) return;
    SDL_FillRect(g_surface, NULL, _rgb(r, g, b));
}

/* 픽셀 한 점 그리기. */
void sdl_pixel(int x, int y, int r, int g, int b) {
    if (!g_surface || x < 0 || y < 0 || x >= g_width || y >= g_height) return;
    SDL_LockSurface(g_surface);
    uint32_t *pixels = (uint32_t *)g_surface->pixels;
    pixels[y * (g_surface->pitch / 4) + x] = _rgb(r, g, b);
    SDL_UnlockSurface(g_surface);
}

/* 채운 사각형 그리기. */
void sdl_rect(int x, int y, int w, int h, int r, int g, int b) {
    if (!g_surface) return;
    SDL_Rect rect = { x, y, w, h };
    SDL_FillRect(g_surface, &rect, _rgb(r, g, b));
}

/* 직선 그리기 (Bresenham). */
void sdl_line(int x0, int y0, int x1, int y1, int r, int g, int b) {
    if (!g_surface) return;
    SDL_LockSurface(g_surface);
    uint32_t *pixels = (uint32_t *)g_surface->pixels;
    int pitch4 = g_surface->pitch / 4;
    uint32_t col = _rgb(r, g, b);
    int dx = abs(x1 - x0), dy = abs(y1 - y0);
    int sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
    int err = dx - dy;
    while (1) {
        if (x0 >= 0 && x0 < g_width && y0 >= 0 && y0 < g_height)
            pixels[y0 * pitch4 + x0] = col;
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 > -dy) { err -= dy; x0 += sx; }
        if (e2 <  dx) { err += dx; y0 += sy; }
    }
    SDL_UnlockSurface(g_surface);
}

/* ---- 입력 ---- */

/* 키 눌림 여부. SDL_SCANCODE_* 값 사용. */
int sdl_key(int scancode) {
    if (!g_keys) return 0;
    return g_keys[scancode] ? 1 : 0;
}

int sdl_mouse_x(void)       { return g_mouse_x;  }
int sdl_mouse_y(void)       { return g_mouse_y;  }
/* 버튼: 1=왼쪽, 2=중간, 3=오른쪽 */
int sdl_mouse_button(int b) { return (g_mouse_btn & SDL_BUTTON(b)) ? 1 : 0; }

/* ---- 타이머 ---- */

/* 밀리초 대기. */
void sdl_delay(int ms) { SDL_Delay((uint32_t)ms); }

/* 프로그램 시작 이후 경과 밀리초. */
int sdl_ticks(void) { return (int)SDL_GetTicks(); }
