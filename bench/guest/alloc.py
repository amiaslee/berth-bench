# 占内存：alloc.py <MiB> <保持秒数> [完成标记文件]——每页都写过，确保真占上
import sys, time, mmap
mib, hold = int(sys.argv[1]), float(sys.argv[2])
mark = sys.argv[3] if len(sys.argv) > 3 else None
m = mmap.mmap(-1, mib << 20)
for i in range(0, mib << 20, 4096):
    m[i] = 1
if mark:
    open(mark, 'w').write('ok')
time.sleep(hold)
