# Resume los reportes IN de la captura de ra100 que saca tshark (el comando está al final de docs/protocolo.md).
import sys, struct, collections
# stdin: frame \t time \t hexdata (interrupt IN completions from dev 65)
cnt = collections.Counter(); knob = collections.defaultdict(list); ball = collections.defaultdict(list)
lens = collections.Counter(); multi = 0; btn = []
for line in sys.stdin:
    f = line.rstrip("\n").split("\t")
    if len(f) < 3 or not f[2]: continue
    d = bytes.fromhex(f[2].replace(":", "")); t = float(f[1])
    cnt[d[0]] += 1; lens[(d[0], len(d))] += 1
    if d[0] == 6:
        v = struct.unpack("<16i", d[1:65]); nz = [(i, x) for i, x in enumerate(v) if x]
        if len(nz) > 1: multi += 1
        for i, x in nz: knob[i].append((t, x))
    elif d[0] == 5:
        v = struct.unpack("<9i", d[1:37])
        for i, x in enumerate(v):
            if x: ball[i].append(x)
    elif d[0] == 2:
        btn.append((t, int.from_bytes(d[1:9], "little")))
print("ids:", dict(cnt)); print("lens:", dict(lens)); print("0x06 con >1 slot != 0:", multi)
for i in sorted(knob):
    xs = [x for _, x in knob[i]]
    print(f"slot {i:2d}: n={len(xs):4d} t={knob[i][0][0]:.1f}-{knob[i][-1][0]:.1f}s min={min(xs)} max={max(xs)} suma={sum(xs)} mod360={set(x % 360 for x in xs)} valores={collections.Counter(xs).most_common(6)}")
for i in sorted(ball):
    xs = ball[i]; print(f"bola slot {i}: n={len(xs)} min={min(xs)} max={max(xs)} mod4096={set(x%4096 for x in xs)}")
prev = 0
for t, b in btn:
    on = [i for i in range(64) if (b >> i) & 1 and not (prev >> i) & 1]
    if on: print(f"  {t:6.2f}s botón bit(s) {on}")
    prev = b
