"""Retire de la prose les caracteres qui trahissent une generation automatique.

Le trait long n'est pas remplace a l'aveugle : selon ce qui le precede il devient
deux-points (quand il introduit une explication) ou virgule (quand il ouvre une
incise). Un remplacement uniforme produisait des enchainements de virgules.
"""
import re
import sys

SUBS = [
    ("→", " to "), ("⇒", " means "), ("≤", "<="), ("≥", ">="),
    ("×", "x"), ("≈", "~"), ("−", "-"),
    ("’", "'"), ("“", '"'), ("”", '"'), ("…", "..."),
    (" ", " "), (" ", " "),
    ("⁰", "0"), ("¹", "1"), ("²", "2"), ("³", "3"),
    ("⁴", "4"), ("⁵", "5"), ("⁶", "6"), ("⁷", "7"),
    ("⁸", "8"), ("⁹", "9"), ("⁻", "-"),
]


def fix_dashes(text):
    out = []
    for line in text.split("\n"):
        # tiret demi-cadratin entre deux chiffres = intervalle
        line = re.sub(r"(\d)\s*–\s*(\d)", r"\1 to \2", line)
        line = line.replace("–", "-")
        # trait long : deux-points s'il introduit une explication, virgule sinon
        while "—" in line:
            i = line.index("—")
            before = line[:i].rstrip()
            after = line[i + 1:].lstrip()
            sep = ":" if (before and before[-1] not in ",:;" and
                          not re.search(r",[^,]*$", before)) else ","
            line = before + sep + " " + after
        out.append(line)
    return "\n".join(out)


def main():
    for path in sys.argv[1:]:
        t = open(path, encoding="utf-8").read()
        orig = t
        t = fix_dashes(t)
        for a, b in SUBS:
            t = t.replace(a, b)
        if t != orig:
            open(path, "w", encoding="utf-8").write(t)
        left = sorted({c for c in t if ord(c) > 127})
        print(f"{path}: {'nettoye' if t != orig else 'inchange'} | "
              f"non-ASCII restants: {left if left else 'aucun'}")


if __name__ == "__main__":
    main()
