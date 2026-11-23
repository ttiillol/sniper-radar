"""Data sources (free public APIs only). Every function returns None/raises on failure;
callers treat missing data as a FAIL (design rule)."""
import json
import time

import requests

import scanner  # reuse the paced, credit-counted Solana RPC client

UA = {"User-Agent": "sniper-radar-desk/1.0"}
SYSTEM_PROGRAM = "11111111111111111111111111111111"


def get_json(url, timeout=15, retries=2):
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 + 3 * i)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            time.sleep(1 + i)
    return None


# ---------------------------------------------------------------- DexScreener
def dex_pairs(mints):
    """{mint: best pair dict} for up to 30 mints. Prefers the PumpSwap pair."""
    out = {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = get_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk))
        if not isinstance(data, list):
            continue
        for p in data:
            m = (p.get("baseToken") or {}).get("address")
            if m not in chunk:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            cur = out.get(m)
            score = (p.get("dexId") == "pumpswap", liq)
            if cur is None or score > (cur.get("dexId") == "pumpswap",
                                       (cur.get("liquidity") or {}).get("usd") or 0):
                out[m] = p
    return out


def pair_price(p):
    try:
        return float(p.get("priceUsd") or 0) or None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- RugCheck / GoPlus
def rugcheck(mint):
    return get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=20)


def goplus(mint):
    d = get_json(f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={mint}")
    try:
        res = d["result"]
        return res.get(mint) or next(iter(res.values()))
    except Exception:
        return None


# ---------------------------------------------------------------- on-chain (RPC)
def mint_info(mint, rpc=scanner.rpc):
    r = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    info = r["value"]["data"]["parsed"]["info"]
    return {"mint_authority": info.get("mintAuthority"), "freeze_authority": info.get("freezeAuthority"),
            "supply": int(info["supply"]), "decimals": int(info["decimals"])}


def top_holders(mint, rpc=scanner.rpc, rc=None):
    """Largest holders as [{owner, amount, is_wallet}] (raw amounts).
    is_wallet=False means the owner is a program account (pool vault, locker...).
    Keyless endpoints often refuse getTokenLargestAccounts -> fall back to RugCheck's holder list."""
    try:
        largest = rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])["value"][:20]
        accts = rpc("getMultipleAccounts", [[a["address"] for a in largest], {"encoding": "jsonParsed"}])["value"]
        rows = []
        for a, acc in zip(largest, accts):
            try:
                owner = acc["data"]["parsed"]["info"]["owner"]
            except Exception:
                owner = None
            rows.append({"owner": owner, "amount": int(a["amount"])})
    except scanner.RpcError:
        hs = (rc or {}).get("topHolders") or []
        if not hs:
            raise
        rows = [{"owner": h.get("owner"), "amount": int(float(h.get("amount") or 0))} for h in hs[:20]]
    owners = [r["owner"] for r in rows if r["owner"]]
    kinds = {}
    if owners:
        oa = rpc("getMultipleAccounts", [owners, {"encoding": "base64"}])["value"]
        for o, acc in zip(owners, oa):
            # wallets are owned by the System Program (or don't exist yet = plain keypair)
            kinds[o] = acc is None or acc.get("owner") == SYSTEM_PROGRAM
    for r in rows:
        r["is_wallet"] = kinds.get(r["owner"], False)
    return rows


# ---------------------------------------------------------------- PumpPortal migrations
async def listen_migrations(on_migration, until):
    import asyncio
    import websockets
    while time.time() < until:
        try:
            async with websockets.connect(scanner.PUMPPORTAL_WS, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                while time.time() < until:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        continue
                    msg = json.loads(raw)
                    if msg.get("mint") and "migrat" in str(msg.get("txType", "migrate")).lower():
                        on_migration(msg)
        except Exception as e:
            scanner.ERR_SAMPLES.setdefault("pumpportal", repr(e)[:140])
            print("migration ws error:", repr(e)[:120], flush=True)
            await asyncio.sleep(5)
