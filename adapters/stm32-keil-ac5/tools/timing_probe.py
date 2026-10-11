#!/usr/bin/env python3
"""突发时序探针 —— 用「真实时间」直接量出 MCU 的实际时钟与 UART 波特率。

为什么需要它：
    之前扫描 300~400000 所有波特率都解不出正确数据，说明瓶颈不是"猜哪个波特率"，
    而是两个未知数同时存在：
        (1) MCU 实际时钟是多少（晶振是不是 25 MHz？）
        (2) PC 端设的波特率和 CH340 芯片实际跑的是否一致？
    时间不会骗人。本脚本只记录"字节到达的墙钟时间"，不依赖解码内容。

固件行为（tools/_patch_burst.py 打过补丁）：
    while(1) { 发 256 字节连续突发; HAL_Delay(1000); }

可推出的两个量（都与 PC 设的波特率无关）：
    T_interval = 相邻突发起点的真实间隔
               = HAL_Delay(1000) 的真实时长 = 168e6 / f_actual 秒
               => f_actual = 168e6 / T_interval
               => HSE 晶振 = 25 MHz / T_interval   (若时钟正确则 T_interval=1.000 s)

    T_burst    = 一次突发首字节 -> 末字节的真实耗时 = 2560 / B_actual
               => B_actual = 2560 / T_burst        (交叉验证，粒度约 ±1 ms)

用法:
    python tools/timing_probe.py            # 默认 COM9, 自适应等待
    python tools/timing_probe.py COM9 40
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
CAP = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0

PC_BAUD = 115200          # PC 端随便设，只影响"解出什么字节"，不影响"什么时候到"
MIN_GAP = 0.15            # 相邻两批数据间隔 > 150 ms 视为两次不同的突发
WANT_BURSTS = 4


def capture():
    s = serial.Serial(PORT, PC_BAUD, timeout=0.005)
    s.reset_input_buffer()
    chunks = []           # [(t, nbytes)]
    buf = b""
    t0 = time.time()
    last_t = -1.0
    n_bursts = 0
    while time.time() - t0 < CAP:
        n = s.in_waiting
        if n:
            data = s.read(n)
            buf += data
            t = time.time() - t0
            if t - last_t > MIN_GAP:
                n_bursts += 1
            chunks.append((t, len(data)))
            last_t = t
            if n_bursts >= WANT_BURSTS:
                break
        else:
            time.sleep(0.001)
    s.close()
    return chunks, buf


def diagnose(chunks, buf):
    """没有间隙时的取证：区分「固件没在停」和「空档里全是噪声」。"""
    import collections
    el = chunks[-1][0] if chunks else 0.0
    print(f"[取证] 收到 {len(buf)} 字节 / {el:.1f}s = {len(buf)/max(el,0.001):,.0f} B/s")
    print(f"[取证] 不同字节值个数 = {len(set(buf))}  (1=恒定, 很大=噪声/乱码)")
    top = collections.Counter(buf).most_common(6)
    print(f"[取证] 出现最多: {[(hex(k), v) for k, v in top]}")
    if len(chunks) > 1:
        gaps = [chunks[i + 1][0] - chunks[i][0] for i in range(len(chunks) - 1)]
        print(f"[取证] 块数 {len(chunks)}  最大间隔 {max(gaps)*1000:.1f} ms")
    print(f"[取证] 前 12 块 (时间ms / 字节数): "
          f"{[(round(t*1000), n) for t, n in chunks[:12]]}")


def group_bursts(chunks):
    bursts = []
    for t, n in chunks:
        if not bursts or (t - bursts[-1][1]) > MIN_GAP:
            bursts.append([t, t, n])      # [start, end, bytes]
        else:
            bursts[-1][1] = t
            bursts[-1][2] += n
    return bursts


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else float("nan")


def main():
    print(f"=== 时序探针: {PORT} @ PC {PC_BAUD} 8N1 ===")
    print("固件：每 1000 tick 发 256 字节突发，然后空闲。")
    print("请确认板子正在跑「突发固件」，不要按复位。\n")

    try:
        chunks, buf = capture()
    except Exception as e:
        print(f"打不开 {PORT}: {e}")
        print("  -> 端口不存在：USB-TTL 模块可能没插好（设备管理器里状态会是 Unknown）。")
        return 1

    total = len(buf)
    bursts = group_bursts(chunks)
    if len(bursts) < 2:
        print(f"只收到 {total} 字节 / {len(bursts)} 段突发，样本不够。\n")
        diagnose(chunks, buf)
        print()
        if total > 2000:
            print("  判读：全程连续、没有空档 —— 说明板子根本没在『停』，")
            print("        或空档期间 RX 悬空拾取了噪声（= 接线/共地不良）。")
        print("  先确认：烧录了 tools/_patch_burst.py 之后的固件吗？")
        return 1

    intervals = [bursts[i + 1][0] - bursts[i][0] for i in range(len(bursts) - 1)]
    durs = [b[1] - b[0] for b in bursts if b[2] > 8]
    bytes_per = [b[2] for b in bursts]

    t_int = median(intervals)
    f_actual = 168e6 / t_int
    hse = 25.0 / t_int
    baud = f_actual / 1458.0          # = (SYSCLK/2) / 729

    print(f"收到 {total} 字节，识别出 {len(bursts)} 段突发。")
    print(f"每段字节数: {bytes_per}")
    print()
    print(f"T_interval (突发周期) = {t_int:.4f} s      <- 最关键的数字")
    if durs:
        t_b = median(durs)
        print(f"T_burst    (突发时长) = {t_b:.4f} s")
        print(f"  => B_actual ≈ 2560 / T_burst = {2560.0 / t_b:,.0f} baud   (交叉验证)")
    print()
    print(f"  => 实际 SYSCLK = {f_actual/1e6:.3f} MHz      (固件假设 168.000 MHz)")
    print(f"  => 硬件 UART 波特率 = {baud:,.0f} baud      (PC 端口要设成这个值)")
    print(f"  => 晶振推算 HSE = {hse:.3f} MHz              (仅供参考)")
    print()
    if abs(t_int - 1.0) < 0.03:
        print("结论：真实时钟与固件假设一致(168 MHz) -> 串口按 115200 即可正常解码。")
    else:
        print(f"结论：真实时钟偏离固件假设 {t_int:.3f} 倍 -> 波特率整体偏移，需修正")
        print("      HSE_VALUE / PLLM（或确认板上晶振实际频率）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
