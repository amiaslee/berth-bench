# 烧一个核：burn.py <秒数>；打印这个进程自己记的 CPU 秒数和墙钟秒数
import sys, time, os
end = time.monotonic() + float(sys.argv[1]); t0 = time.monotonic(); n = 0
while time.monotonic() < end:
    n += 1
t = os.times()
print(f"cpu={t.user + t.system:.4f} wall={time.monotonic() - t0:.4f}")
