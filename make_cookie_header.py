import json, time

cookies = json.load(open("session.json", "r", encoding="utf-8"))
now = time.time()
pairs = []
for c in cookies:
    exp = c.get("expires")
    if isinstance(exp, (int, float)) and exp != -1 and exp < now:
        continue
    n = c.get("name")
    v = c.get("value")
    if n and v:
        pairs.append(f"{n}={v}")

print("; ".join(pairs))
