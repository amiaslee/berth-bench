# 两台环境之间一次来回的延迟（模拟 netplay 联机服务到 Pages 规则网关）。
# rtt.py serve <口>                      同一个口上 TCP 和 UDP 都回声；两种都绑好以后才写就绪文件
# rtt.py ping <地址> <口> <tcp|udp> <次数> <字节>   先热身 1000 次不算；打印 p50/p90/p99/p999/max（微秒）、超时次数
# 每个报文带序号，只认对得上的回声（迟到的旧回声丢掉，不算成下一包的低延迟）。
# 两头都是 Python，量出来的数里含 Python 自己的几十微秒，是偏保守的上界。
import socket, struct, sys, threading, time

def serve(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port)); s.listen(16)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("0.0.0.0", port))
    def echo(c):
        while True:
            b = c.recv(65536)
            if not b:
                return
            c.sendall(b)
    def tcp():
        while True:
            c, _ = s.accept(); c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=echo, args=(c,), daemon=True).start()
    threading.Thread(target=tcp, daemon=True).start()
    open("/tmp/rtt.ready", "w").write("ok")
    while True:
        b, a = u.recvfrom(65536); u.sendto(b, a)

def ping(host, port, proto, n, size):
    pad = b"x" * max(0, size - 8)
    if proto == "tcp":
        s = socket.create_connection((host, port), timeout=5); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        def one(seq):
            msg = struct.pack("!Q", seq) + pad
            s.sendall(msg); got = b""
            while len(got) < len(msg):
                b = s.recv(65536)
                if not b:
                    raise ConnectionError("对端关了连接")
                got += b
            if struct.unpack("!Q", got[:8])[0] != seq:
                raise ConnectionError("TCP 回声序号对不上")
            return True
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect((host, port)); s.settimeout(0.5)
        def one(seq):
            s.send(struct.pack("!Q", seq) + pad)
            while True:
                b = s.recv(65536)            # 超时抛 socket.timeout
                if struct.unpack("!Q", b[:8])[0] == seq:
                    return True              # 序号不对的是迟到的旧回声，丢掉接着等
    seq = 0
    for _ in range(1000):
        seq += 1
        try:
            one(seq)
        except socket.timeout:
            pass
    xs = []; lost = 0
    for _ in range(n):
        seq += 1
        t = time.perf_counter_ns()
        try:
            one(seq)
        except socket.timeout:
            lost += 1; continue
        xs.append((time.perf_counter_ns() - t) / 1000)
    xs.sort()
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    print(f"n={len(xs)} lost={lost} p50={q(.5):.1f} p90={q(.9):.1f} p99={q(.99):.1f} p999={q(.999):.1f} max={xs[-1]:.1f}")

if sys.argv[1] == "serve":
    serve(int(sys.argv[2]))
else:
    ping(sys.argv[2], int(sys.argv[3]), sys.argv[4], int(sys.argv[5]), int(sys.argv[6]))
