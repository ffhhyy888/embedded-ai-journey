/* ============================================================
 * Stage 6 —— 宿主机单元测试：环形缓冲
 * ============================================================
 * 关键点：include 的是 Core/Inc/ringbuf.h —— **固件用的同一份代码**，
 *        不是复制出来的副本。测副本是经典陷阱。
 *
 * 用宿主机编译器（zig cc）编译执行，不需要板子，毫秒级，可大量重复。
 * 退出码：0 = 全过，非 0 = 有失败用例。
 */
#include <stdint.h>
#include <stdio.h>

#include "ringbuf.h"

#define SZ 8

static uint8_t   mem[SZ];
static ringbuf_t rb;
static int       fails = 0;

/* 失败时只打印前若干条：回绕用例会跑 200 轮，一个取模错误能刷出上千行，
   把真正有用的信息淹掉。计数照常累加，末尾给总数。 */
#define MAX_PRINT 8
#define CHECK(cond, msg)                                    \
    do {                                                    \
        if (!(cond)) {                                      \
            if (fails < MAX_PRINT) {                        \
                printf("  FAIL: %s\n", (msg));              \
            }                                               \
            fails++;                                        \
        }                                                   \
    } while (0)

static void test_empty(void)
{
    uint8_t v;
    rb_init(&rb, mem, SZ);
    CHECK(rb_empty(&rb), "新建后应为空");
    CHECK(rb_count(&rb) == 0, "新建后计数应为 0");
    CHECK(!rb_pop(&rb, &v), "空缓冲 pop 应失败");
}

static void test_fifo(void)
{
    uint8_t v;
    rb_init(&rb, mem, SZ);
    CHECK(rb_push(&rb, 0x11), "push 0x11");
    CHECK(rb_push(&rb, 0x22), "push 0x22");
    CHECK(rb_count(&rb) == 2, "计数应为 2");
    CHECK(rb_pop(&rb, &v) && v == 0x11, "先出 0x11（FIFO 顺序）");
    CHECK(rb_pop(&rb, &v) && v == 0x22, "再出 0x22");
    CHECK(rb_empty(&rb), "取空后应为空");
}

static void test_full_rejects(void)
{
    uint8_t v;
    int i;
    rb_init(&rb, mem, SZ);
    for (i = 0; i < SZ - 1; i++) {
        CHECK(rb_push(&rb, (uint8_t)i), "填满过程中 push 应成功");
    }
    CHECK(rb_count(&rb) == SZ - 1, "满时计数应为 SZ-1（留一格区分满/空）");
    /* 满时再压必须被拒，且不能破坏已有数据 */
    CHECK(!rb_push(&rb, 0xEE), "满时 push 应被拒绝");
    CHECK(rb_count(&rb) == SZ - 1, "被拒后计数不变");
    CHECK(rb_pop(&rb, &v) && v == 0, "被拒后最早的数据仍是 0（没被覆盖）");
}

static void test_wraparound(void)
{
    uint8_t v;
    int round, i;
    rb_init(&rb, mem, SZ);
    /* 反复填满再清空，逼索引绕回；& (size-1) 写错这里必炸 */
    for (round = 0; round < 200; round++) {
        for (i = 0; i < SZ - 1; i++) {
            CHECK(rb_push(&rb, (uint8_t)(round * 7 + i)), "回绕时 push");
        }
        CHECK(rb_count(&rb) == SZ - 1, "回绕时计数");
        for (i = 0; i < SZ - 1; i++) {
            CHECK(rb_pop(&rb, &v), "回绕时 pop");
            CHECK(v == (uint8_t)(round * 7 + i), "回绕时数据顺序/内容");
        }
    }
}

static void test_interleaved(void)
{
    uint8_t v;
    rb_init(&rb, mem, SZ);
    /* 边压边取，模拟"中断收 + 主循环发"的真实节奏 */
    CHECK(rb_push(&rb, 1), "push 1");
    CHECK(rb_push(&rb, 2), "push 2");
    CHECK(rb_pop(&rb, &v) && v == 1, "pop 1");
    CHECK(rb_push(&rb, 3), "push 3");
    CHECK(rb_push(&rb, 4), "push 4");
    CHECK(rb_pop(&rb, &v) && v == 2, "pop 2");
    CHECK(rb_pop(&rb, &v) && v == 3, "pop 3");
    CHECK(rb_pop(&rb, &v) && v == 4, "pop 4");
    CHECK(rb_empty(&rb), "最后应为空");
}

int main(void)
{
    printf("ringbuf host tests (SZ=%d)\n", SZ);
    test_empty();
    test_fifo();
    test_full_rejects();
    test_wraparound();
    test_interleaved();
    if (fails) {
        printf("RESULT: %d CHECK(s) FAILED\n", fails);
        return 1;
    }
    printf("RESULT: ALL OK\n");
    return 0;
}
