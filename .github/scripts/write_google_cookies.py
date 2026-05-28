"""
Normalise Google/Gemini cookies from the GEMINI_COOKIES secret into the
JSON format expected by gemini_web_browser.py.

The secret may be:
  - A JSON array  (from EditThisCookie / Cookie-Editor browser extension)
  - A semicolon-separated string  (name=value; name=value ...)
  - Either of the above base64-encoded

Required cookies for Gemini to work (export from a logged-in Chrome tab on
gemini.google.com):
  __Secure-1PSID, __Secure-3PSID, SAPISID, SID, SSID, HSID, APISID,
  __Secure-1PAPISID, __Secure-3PAPISID, NID

HOW TO EXPORT:
  1. Open Chrome, sign in to your Google account, visit gemini.google.com
  2. Install the "Cookie-Editor" extension (or EditThisCookie)
  3. Click the extension icon → Export → JSON
  4. Paste the entire JSON array as the GEMINI_COOKIES GitHub secret

NOTE: Google cookies expire after roughly 6–12 months.  Re-export when the
agent starts getting "Couldn't sign you in" again.
"""
import os, sys, json, base64, time

REQUIRED = {"__Secure-1PSID", "SAPISID", "SID"}
OUT_PATH  = os.path.expanduser("~/.secrets/gemini_cookies.json")

raw = os.environ.get("GEMINI_COOKIES", "").strip()
if not raw:
    print("GEMINI_COOKIES is empty — skipping.")
    sys.exit(0)

# ── 1. Try base64 decode ───────────────────────────────────────────────────
try:
    decoded = base64.b64decode(raw).decode("utf-8").strip()
    if decoded.startswith("[") or decoded.startswith("{") or "=" in decoded:
        raw = decoded
except Exception:
    pass

cookies: list = []

# ── 2. Try JSON array ──────────────────────────────────────────────────────
try:
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        now = int(time.time())
        skipped = 0
        for c in parsed:
            name  = c.get("name", "").strip()
            value = c.get("value", "")
            if not name:
                continue

            domain = c.get("domain", ".google.com")
            # Ensure Google cookies have the leading dot for subdomain matching
            if domain and not domain.startswith(".") and "google.com" in domain:
                domain = ".google.com"

            path     = c.get("path", "/")
            secure   = bool(c.get("secure", True))
            httpOnly = bool(c.get("httpOnly", False))

            same_site_raw = c.get("sameSite", c.get("same_site", "None"))
            same_site_map = {"no_restriction": "None", "lax": "Lax", "strict": "Strict",
                             "none": "None", "unspecified": "None"}
            same_site = same_site_map.get(str(same_site_raw).lower(), "None")

            raw_exp = c.get("expirationDate") or c.get("expires")
            if raw_exp is None:
                expires = 2147483647          # session cookie — no expiry
            else:
                try:
                    exp_int = int(float(raw_exp))
                    if exp_int > 0 and exp_int < now:
                        skipped += 1
                        continue                # genuinely expired — skip
                    expires = exp_int if exp_int > 0 else 2147483647
                except (TypeError, ValueError):
                    expires = 2147483647

            cookies.append({
                "name":     name,
                "value":    value,
                "domain":   domain,
                "path":     path,
                "secure":   secure,
                "httpOnly": httpOnly,
                "sameSite": same_site,
                "expires":  expires,
            })

        msg = f"Google cookies: JSON → normalised ({len(cookies)} written"
        if skipped:
            msg += f", {skipped} expired skipped"
        print(msg + ").")

    else:
        raise ValueError("Not a JSON array")

except Exception:
    # ── 3. Semicolon-separated fallback  (name=value; ...) ─────────────────
    cookies = []
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies.append({
                "name":     k.strip(),
                "value":    v.strip(),
                "domain":   ".google.com",
                "path":     "/",
                "secure":   True,
                "httpOnly": False,
                "sameSite": "None",
                "expires":  2147483647,
            })
    print(f"Google cookies: semicolon-string → normalised ({len(cookies)} written).")

# ── 4. Validate required cookies ──────────────────────────────────────────
present = {c["name"] for c in cookies}
missing = REQUIRED - present
if missing:
    print(
        f"WARNING: missing critical Google auth cookies: {missing}\n"
        "Gemini login will likely fail.  Re-export cookies from a fresh\n"
        "Chrome session on gemini.google.com and update the GEMINI_COOKIES secret."
    )

# ── 5. Write output ────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
with open(OUT_PATH, "w") as f:
    json.dump(cookies, f, indent=2)
print(f"Written: {OUT_PATH}")
