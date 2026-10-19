"""Extraction de features depuis une transaction de deploiement pump.fun brute (jsonParsed).

CONTRAINTE t_decision : tout ce qui sort d'ici est contenu dans la transaction de
deploiement elle-meme, donc connu au slot du deploiement. Aucune donnee posterieure.
"""
import json

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_MAP = {c: i for i, c in enumerate(B58_ALPHABET)}

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Discriminants Anchor observes dans les deploiements pump.fun
DISC_CREATE = bytes.fromhex("d6904cec5f8b31b4")
# Deux familles d'achat coexistent : "je veux N tokens, au plus X SOL" et
# "je depense X SOL, au moins N tokens". Chaque famille a plusieurs variantes de
# programme ; le lamport a retenir n'est pas au meme offset selon la famille.
DISC_BUY_TOKENS_OUT = frozenset([
    bytes.fromhex("66063d1201daebea"),
    bytes.fromhex("b817ee6167c5d33d"),
])
DISC_BUY_SOL_IN = frozenset([
    bytes.fromhex("38fc74089edfcd5f"),
    bytes.fromhex("c2ab1c46684d5b2f"),
])
DISC_EVENT_CPI = bytes.fromhex("e445a52e51cb9a1d")  # log Anchor, jamais un achat

JITO_TIP_ACCOUNTS = frozenset([
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
])


def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + _B58_MAP[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + body


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(B58_ALPHABET[r])
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + "".join(reversed(out))


INT64_MAX = 2**63 - 1


def _u64(raw, off):
    """Un u64 peut depasser int64 et faire exploser l'ecriture parquet.
    Les valeurs concernees sont des decodages aberrants : on les borne."""
    v = int.from_bytes(raw[off:off + 8], "little")
    return v if v <= INT64_MAX else INT64_MAX


def _read_borsh_string(raw, off):
    ln = int.from_bytes(raw[off:off + 4], "little")
    off += 4
    return raw[off:off + ln].decode("utf-8", "replace"), off + ln


def _uri_host(uri):
    if not uri or "://" not in uri:
        return ""
    return uri.split("://", 1)[1].split("/", 1)[0].lower()


def _cb_params(raw):
    """ComputeBudget : 0x02 = SetComputeUnitLimit(u32), 0x03 = SetComputeUnitPrice(u64)."""
    if not raw:
        return None, None
    tag = raw[0]
    if tag == 2 and len(raw) >= 5:
        return int.from_bytes(raw[1:5], "little"), None
    if tag == 3 and len(raw) >= 9:
        return None, int.from_bytes(raw[1:9], "little")
    return None, None


def parse_tx(d):
    msg = d["transaction"]["message"]
    meta = d["meta"]
    keys = msg["accountKeys"]
    pubkeys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]

    signer = next((k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")), pubkeys[0])
    # Le mint est accounts[0] de l'instruction create. On ne peut PAS le deviner
    # via le suffixe "pump" : 3% des mints ne sont pas des adresses vanity.
    mint = None

    name = symbol = uri = ""
    creator = None
    dev_buy_lamports = 0
    dev_buy_tokens = 0
    n_pump_ix = 0
    cu_limit = cu_price = None
    jito_tip = 0

    all_ix = list(msg["instructions"])
    for grp in meta.get("innerInstructions") or []:
        all_ix.extend(grp.get("instructions", []))

    for ix in all_ix:
        pid = ix.get("programId")
        if pid == PUMP_PROGRAM and "data" in ix:
            raw = b58decode(ix["data"])
            disc = raw[:8]
            if disc == DISC_EVENT_CPI:
                continue
            n_pump_ix += 1
            if disc == DISC_CREATE:
                acc = ix.get("accounts") or []
                if acc:
                    a0 = acc[0]
                    mint = pubkeys[a0] if isinstance(a0, int) else a0
                off = 8
                name, off = _read_borsh_string(raw, off)
                symbol, off = _read_borsh_string(raw, off)
                uri, off = _read_borsh_string(raw, off)
                if len(raw) - off >= 32:
                    creator = b58encode(raw[off:off + 32])
            elif disc in DISC_BUY_TOKENS_OUT and len(raw) >= 24:
                dev_buy_tokens = _u64(raw, 8)
                dev_buy_lamports = _u64(raw, 16)
            elif disc in DISC_BUY_SOL_IN and len(raw) >= 24:
                dev_buy_lamports = _u64(raw, 8)
                dev_buy_tokens = _u64(raw, 16)
        elif pid == COMPUTE_BUDGET and "data" in ix:
            lim, price = _cb_params(b58decode(ix["data"]))
            if lim is not None:
                cu_limit = lim
            if price is not None:
                cu_price = price
        elif pid == SYSTEM_PROGRAM and "parsed" in ix:
            p = ix["parsed"]
            if isinstance(p, dict) and p.get("type") in (None, "transfer"):
                info = p.get("info", {})
                if info.get("destination") in JITO_TIP_ACCOUNTS:
                    jito_tip = min(jito_tip + int(info.get("lamports", 0) or 0), INT64_MAX)

    pre = meta.get("preBalances") or [0]
    post = meta.get("postBalances") or [0]
    signer_pre = pre[0] if pre else 0
    signer_spent = (pre[0] - post[0]) if (pre and post) else 0

    low = name.lower()
    return {
        "slot": d["slot"],
        "block_time": d["blockTime"],
        "tx_index": d.get("transactionIndex"),
        "mint": mint,
        "signer": signer,
        "creator": creator or signer,
        "mint_is_vanity": int(bool(mint) and mint.endswith("pump")),
        "creator_is_signer": int((creator or signer) == signer),
        "name": name,
        "symbol": symbol,
        "uri_host": _uri_host(uri),
        # --- forme du nom / ticker ---
        "name_len": len(name),
        "symbol_len": len(symbol),
        "name_eq_symbol": int(low == symbol.lower()),
        "name_is_upper": int(name.isupper() and bool(name)),
        "name_has_digit": int(any(c.isdigit() for c in name)),
        "name_has_space": int(" " in name),
        "name_non_ascii": int(any(ord(c) > 127 for c in name)),
        "name_n_words": len(name.split()),
        # --- economie du deploiement ---
        "dev_buy_lamports": dev_buy_lamports,
        "dev_buy_tokens": dev_buy_tokens,
        "signer_pre_lamports": signer_pre,
        "signer_spent_lamports": signer_spent,
        "fee": meta.get("fee", 0),
        "cu_consumed": meta.get("computeUnitsConsumed", 0),
        "cu_limit": cu_limit or 0,
        "cu_price": cu_price or 0,
        "jito_tip_lamports": jito_tip,
        "is_bundled": int(jito_tip > 0),
        # --- forme de la transaction ---
        "n_instructions": len(msg["instructions"]),
        "n_inner_groups": len(meta.get("innerInstructions") or []),
        "n_accounts": len(pubkeys),
        "n_signatures": len(d["transaction"]["signatures"]),
        "n_alt_lookups": len(msg.get("addressTableLookups") or []),
        "n_pump_ix": n_pump_ix,
        "tx_err": int(meta.get("err") is not None),
    }


def parse_line(line):
    try:
        return parse_tx(json.loads(line))
    except Exception:
        return None
