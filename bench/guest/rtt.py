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

# ---- H19 的量法（netplay 2026-11-20 定）：请求小、回包按指定大小；规则自己算的时间带回来，客户端减掉 ----
# rtt.py serve2 <口>                         每个请求 16 字节（序号、要回多少字节）；回包头 16 字节（序号、规则计算纳秒）+ 填充
# rtt.py ping2 <地址> <口> <次数> <回包字节>   先热身 1000 次；打印扣掉规则计算以后的 p50/p90/p99/p999/max（微秒）

def serve2(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port)); s.listen(16)
    def handle(c):
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        while True:
            req = b""
            while len(req) < 16:
                b = c.recv(16 - len(req))
                if not b:
                    return
                req += b
            t0 = time.perf_counter_ns()
            seq, n = struct.unpack("!QQ", req)
            body = b"r" * max(0, n - 16)
            c.sendall(struct.pack("!QQ", seq, time.perf_counter_ns() - t0) + body)
    open("/tmp/rtt2.ready", "w").write("ok")
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()

def ping2(host, port, n, size):
    s = socket.create_connection((host, port), timeout=10); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.sendall(b"")  # 带票：真的网关在连接时验一次票，这里省了（每条消息不验）
    def one(seq):
        s.sendall(struct.pack("!QQ", seq, size))
        got = b""
        while len(got) < max(16, size):
            b = s.recv(1 << 20)
            if not b:
                raise ConnectionError("对端关了连接")
            got += b
        rseq, proc = struct.unpack("!QQ", got[:16])
        if rseq != seq:
            raise ConnectionError("回包序号对不上")
        return proc
    for k in range(1000):
        one(k)
    xs = []
    for k in range(1000, 1000 + n):
        t = time.perf_counter_ns()
        proc = one(k)
        xs.append((time.perf_counter_ns() - t - proc) / 1000)
    xs.sort()
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    print(f"n={len(xs)} lost=0 p50={q(.5):.1f} p90={q(.9):.1f} p99={q(.99):.1f} p999={q(.999):.1f} max={xs[-1]:.1f}")

if sys.argv[1] == "serve":
    serve(int(sys.argv[2]))
elif sys.argv[1] == "serve2":
    serve2(int(sys.argv[2]))
elif sys.argv[1] == "ping2":
    ping2(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
else:
    ping(sys.argv[2], int(sys.argv[3]), sys.argv[4], int(sys.argv[5]), int(sys.argv[6]))
