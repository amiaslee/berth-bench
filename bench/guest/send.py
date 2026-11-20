# 往宿主发固定字节数：send.py <地址> <口> <MiB>；打印实际发出的应用层字节
import socket, sys
host, port, mib = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
buf = b'\0' * (1 << 20); s = socket.create_connection((host, port)); n = 0
for _ in range(mib):
    s.sendall(buf); n += len(buf)
s.shutdown(socket.SHUT_WR); s.recv(1); s.close()
print(f"sent={n}")
