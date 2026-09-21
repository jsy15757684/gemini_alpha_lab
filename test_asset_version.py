#!/usr/bin/env python3
"""정적 자산을 고치고 ?v= 버전을 안 올리는 실수를 잡는다.

index.html 은 매번 새로 내려가지만 app.js·style.css 는 ?v= 가 그대로면
브라우저가 캐시를 쓴다. 그래서 **새 DOM 에 옛 JS** 가 붙어, 화면은 떴는데
아무것도 안 채워지는 상태가 된다. 실제로 그렇게 나갔다 — 기어·증시·세금
패널이 들어간 커밋이 v=5.0.4 그대로였다.

파일 내용의 해시를 버전과 함께 적어둔다. 파일을 고치면 해시가 달라지므로,
버전을 같이 올리지 않으면 이 검사가 막는다.
"""

import hashlib
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

# {자산 경로: (index.html 에 적힌 버전, 그 버전일 때의 내용 sha256)}
EXPECTED = {
    "static/js/app.js":    ("5.0.8", "c15fa3604f13de9493acfee050484ab0"),
    "static/css/style.css": ("4.9.0", "f7cc288157e561b4b93f9a3817005092"),
}

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {len(PASS) + len(FAIL):2d}. {name}  — {detail}")


html = open(os.path.join(BASE, "static/index.html"), encoding="utf-8").read()

for rel, (want_ver, want_hash) in EXPECTED.items():
    ref = os.path.basename(rel)
    m = re.search(rf"{re.escape(ref)}\?v=([0-9.]+)", html)
    check(f"{ref} 의 ?v= 가 index.html 에 있다", bool(m), m.group(1) if m else "없음")
    if not m:
        continue
    check(f"{ref} 버전이 기록과 같다 ({want_ver})", m.group(1) == want_ver,
          f"index.html={m.group(1)} · 기록={want_ver}")

    digest = hashlib.sha256(
        open(os.path.join(BASE, rel), "rb").read()).hexdigest()[:32]
    if not want_hash:
        check(f"{ref} 해시 기록 (참고)", True, digest)
        continue
    ok = digest == want_hash
    check(f"{ref} 내용이 바뀌었으면 버전도 올렸다", ok,
          digest if ok else
          f"내용 해시 {digest} ≠ 기록 {want_hash} → index.html 의 ?v= 와 "
          f"이 파일의 EXPECTED 를 같이 올리세요")

print(f"\n{'=' * 58}")
if FAIL:
    print(f"통과 {len(PASS)}개 · 실패 {len(FAIL)}개")
    for f in FAIL:
        print(f"  ❌ {f}")
    sys.exit(1)
print(f"전체 {len(PASS)}개 항목 통과")
