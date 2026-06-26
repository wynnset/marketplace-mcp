import unicodedata, re, os

# Paths are resolved relative to this script (scripts/), not the CWD: the input
# GeoNames dump sits next to it, the output gazetteer goes into ../src/.
HERE = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(HERE, "cities1000.txt")   # download first (see header note)
OUTPUT = os.path.join(HERE, "..", "src", "gazetteer.py")

def strip(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().strip()
    s = re.sub(r"^st\.?\s+", "saint ", s)   # St. Louis / St Louis -> saint louis
    return s

def collect(target_cc):
    best = {}
    with open(INPUT, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 15: continue
            if p[8] != target_cc: continue
            key = strip(p[2])
            if not key: continue
            try: pop = int(p[14] or 0)
            except: pop = 0
            if key not in best or pop > best[key][0]:
                best[key] = (pop, round(float(p[4]),4), round(float(p[5]),4))
    return best

us = collect("US"); ca = collect("CA")
us_top = dict(sorted(us.items(), key=lambda kv: kv[1][0], reverse=True)[:1000])
ca_top = dict(sorted(ca.items(), key=lambda kv: kv[1][0], reverse=True)[:500])
merged = {}
for src in (us_top, ca_top):
    for k,v in src.items():
        if k not in merged or v[0] > merged[k][0]:
            merged[k] = v
# FB-display aliases for names that differ from GeoNames asciiname
if "new york city" in merged: merged.setdefault("new york", merged["new york city"])

lines = [f'    {k!r}: ({v[1]}, {v[2]}),' for k,v in sorted(merged.items())]
with open(OUTPUT,"w",encoding="utf-8") as f:
    f.write('"""North American city centroids (lat, lng), keyed by accent-stripped\n')
    f.write("lowercase city name (with 'St.'->'Saint' normalized). Auto-generated from\n")
    f.write("the GeoNames cities1000 dataset (public domain): top 1000 US + top 500\n")
    f.write("Canadian cities by population, higher-population name winning on collision.\n")
    f.write('Offline fallback used by server._geocode. Regenerate with build_gaz.py."""\n\n')
    f.write("CITIES = {\n" + "\n".join(lines) + "\n}\n")
print("wrote", OUTPUT, "| keys:", len(merged), "bytes:", os.path.getsize(OUTPUT))
for probe in ("new york","new york city","saint louis","saint paul","vancouver","montreal","oklahoma city"):
    print("  ", probe, "->", merged.get(probe))
