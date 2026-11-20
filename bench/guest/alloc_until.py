"""每次占 16 MiB，写过每页后报累计 MiB，直到进程被杀。"""
import mmap


def main():
    blocks = []
    size = 16 << 20
    while True:
        block = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
        for offset in range(0, size, mmap.PAGESIZE):
            block[offset] = 1
        blocks.append(block)
        print(16 * len(blocks), flush=True)


if __name__ == "__main__":
    main()
