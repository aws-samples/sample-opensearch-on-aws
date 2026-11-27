"""Fill the {{PLACEHOLDER}} values from config.env into every file under templates/, pipeline/, iam/, collector/,
demo/ and write the result to rendered/. Unfilled placeholders are reported and fail the run."""
import os, re, sys, shutil
if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg_path = os.path.join(ROOT, "config.env")
if not os.path.exists(cfg_path):
    sys.exit("config.env not found; copy config.env.example to config.env and fill it in")
cfg = {}
for line in open(cfg_path, encoding="utf-8"):
    line = line.split("#", 1)[0].strip()   # drop comments, including a trailing "KEY=value   # note"
    if "=" in line:
        k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
out = os.path.join(ROOT, "rendered"); shutil.rmtree(out, ignore_errors=True)
missing = set()
for top in ("templates", "pipeline", "iam", "collector", "demo"):
    for dirpath, _, files in os.walk(os.path.join(ROOT, top)):
        for f in files:
            src = os.path.join(dirpath, f); rel = os.path.relpath(src, ROOT)
            text = open(src, encoding="utf-8").read()
            def sub(m):
                k = m.group(1)
                if k in cfg: return cfg[k]
                missing.add(f"{rel}: {k}"); return m.group(0)
            text = re.sub(r"\{\{([A-Z_]+)\}\}", sub, text)
            dst = os.path.join(out, rel); os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "w", encoding="utf-8", newline="\n").write(text)
print(f"rendered to {out}")
if missing:
    print("UNFILLED placeholders:"); [print("  ", m) for m in sorted(missing)]; sys.exit(1)
