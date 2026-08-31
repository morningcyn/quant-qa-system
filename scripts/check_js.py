# 简易 JS 括号平衡检查（字符串/注释剔除后）
import re, sys

src = open(sys.argv[1], encoding='utf-8').read()
s = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', '', src)
s = re.sub(r'//[^\n]*|/\*.*?\*/', '', s, flags=re.S)
ok = True
for a, b in [('(', ')'), ('{', '}'), ('[', ']')]:
    ca, cb = s.count(a), s.count(b)
    print(f'{a} {ca} / {b} {cb}', 'OK' if ca == cb else 'MISMATCH')
    ok = ok and ca == cb
sys.exit(0 if ok else 1)
