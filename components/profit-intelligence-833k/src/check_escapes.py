"""Verifie que les cellules de code du generateur de notebook n'ont pas de
backslash-n simple : dans un bloc triple-quote, Python le convertirait en vrai
saut de ligne a la generation et casserait la f-string du notebook."""
import re
import sys

SINGLE = re.compile(r'(?<!\\)\\n')

path = sys.argv[1] if len(sys.argv) > 1 else "src/make_notebook.py"
text = open(path, encoding="utf-8").read()

bad = []
for i, line in enumerate(text.split("\n"), start=1):
    for m in SINGLE.finditer(line):
        # un backslash-n double s'ecrit \\n : on ne veut signaler que les simples
        if line[max(0, m.start() - 1):m.start()] != "\\":
            bad.append((i, line.strip()[:96]))
            break

if bad:
    print(f"{len(bad)} ligne(s) a corriger (backslash-n simple) :")
    for i, l in bad:
        print(f"  {i:>4}| {l}")
    sys.exit(1)

print("OK : aucun backslash-n simple dans le generateur")

non_ascii = sorted({c for c in text if ord(c) > 127})
print("caracteres non-ASCII :", non_ascii if non_ascii else "aucun")
