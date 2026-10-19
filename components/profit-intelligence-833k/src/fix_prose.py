"""Repare les tournures cassees par la substitution automatique des traits longs.

La regle "deux-points quand il introduit une explication" se trompe quand le
trait long ouvrait une incise ou tombait dans une cellule de tableau. On liste
donc les cas un par un plutot que d'appliquer une seconde regle aveugle.
"""
import sys

FIX = [
    ("positions closed in 3 or more: and closes 100% of positions.",
     "positions closed in 3 or more, and closes 100% of positions."),
    ("prediction: precisely because the bot enters 118 transactions late",
     "prediction, precisely because the bot enters 118 transactions late"),
    ("so a drop means restarting from byte 0, across 34 hours",
     "so a drop means restarting from byte 0. Across 34 hours"),
    ("The negative member then landed whole: **14,955,899,885 bytes, byte-exact\nagainst the tar header**: parsing to",
     "The negative member then landed whole (**14,955,899,885 bytes, byte-exact\nagainst the tar header**), parsing to"),
    ("|: of which bought by the bot |", "| of which bought by the bot |"),
    ('would be gross leakage: the model would learn "has history  means \npositive". Nothing here uses it.',
     "would be gross leakage: the model would learn that merely having a history\nimplies a buy. Nothing here uses it."),
    ("### The deployer is the signal: not the token",
     "### The deployer is the signal, not the token"),
    ("of its own money did it stake: and how many did the bot already take",
     "of its own money did it stake, and how many did the bot already take"),
    ("deployments it ignored**: x6.2 from a single binary.",
     "deployments it ignored**, a x6.2 lift from a single binary."),
    ("wallet's own behaviour: and they are nearly as strong:",
     "wallet's own behaviour, and they are nearly as strong:"),
    ("still leaves **0.102**: so roughly half the predictive power owes nothing to",
     "still leaves **0.102**, so roughly half the predictive power owes nothing to"),
    ("overlap**: dropping", "overlap**. Dropping"),
    ("0.2108  to  0.1032", "0.2108 to 0.1032"),
    ("**career beats economics four to one**: who the deployer is",
     "**career beats economics four to one**. Who the deployer is"),
    ("- **Monotonic constraints: and a result of ours that reversed.**",
     "- **Monotonic constraints, and a result of ours that reversed.**"),
    ('"more dev-buy  means  more likely bought"', '"more dev-buy implies more likely bought"'),
    ("roughly **3 ROI points**: real, but it does not carry the result.",
     "roughly **3 ROI points**, real but not what carries the result."),
    ("would separate the two: and would lower the headline.",
     "would separate the two, and would lower the headline."),
    ("|: |", "| |"),
    ("rho = ", "rho = "),
    ("ρ", "rho"),
]


def main():
    for path in sys.argv[1:]:
        t = open(path, encoding="utf-8").read()
        n = 0
        for a, b in FIX:
            if a in t:
                t = t.replace(a, b)
                n += 1
        open(path, "w", encoding="utf-8").write(t)
        left = sorted({c for c in t if ord(c) > 127})
        print(f"{path}: {n} corrections | non-ASCII: {left if left else 'none'}")


if __name__ == "__main__":
    main()
