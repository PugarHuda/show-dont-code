"""Read a published protocol from protocols.io (by link, URI or DOI) as plain text for Muse Spark: title, abstract,
materials and every step, in order. Uses the public API with the app's client token."""
import html
import json
import os
import re
import urllib.parse
import urllib.request

PIO = "https://www.protocols.io/api"
UA = "show-dont-code/1.0 (+https://github.com/PugarHuda/show-dont-code)"  # Cloudflare refuses Python's default (1010)


def _get(path, token):
    req = urllib.request.Request(PIO + path, headers={"Authorization": f"Bearer {token}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    if out.get("status_code", 0) != 0:
        raise ValueError(out.get("error_message") or "protocols.io refused the request")
    return out["payload"]


def ref(link):
    """protocols.io link, URI or DOI -> the id the API takes. Only protocols.io addresses are accepted."""
    link = link.strip()
    m = re.match(r"^(?:https?://)?(?:dx\.)?doi\.org/(10\.17504/protocols\.io\.[\w.]+(?:/v\d+)?)$", link) or \
        re.match(r"^(10\.17504/protocols\.io\.[\w.]+(?:/v\d+)?)$", link)
    if m:
        return urllib.parse.quote(m[1], safe="")  # the API takes a DOI as the id
    m = re.match(r"^(?:https?://)?(?:www\.)?protocols\.io/(?:view|edit|private)/([\w-]+)", link) or re.match(r"^([\w-]{3,200})$", link)
    if not m:
        raise ValueError("give a protocols.io link, URI or DOI")
    return m[1]


def _plain(s):
    s = re.sub(r"<[^>]+>", " ", str(s or "")).replace("%s", " ")
    return re.sub(r"[ \t]+", " ", html.unescape(s)).strip()


def fetch(link, token=None):
    """{"title", "doi", "url", "text"}: the protocol as Muse Spark reads it."""
    token = token or os.getenv("PROTOCOLSIO_TOKEN")
    if not token:
        raise RuntimeError("PROTOCOLSIO_TOKEN is not set")
    pid = ref(link)
    p = _get(f"/v4/protocols/{pid}?content_format=markdown", token)
    steps = _get(f"/v4/protocols/{p['id']}/steps?content_format=markdown", token)
    # the API returns steps as a linked list (previous_guid); put them in protocol order
    by_prev = {s.get("previous_guid"): s for s in steps}
    ordered, cur = [], by_prev.get(None)
    while cur and len(ordered) < len(steps):
        ordered.append(cur)
        cur = by_prev.get(cur.get("guid"))
    ordered += [s for s in steps if s not in ordered]
    parts = [f"Title: {_plain(p.get('title'))}", f"Abstract: {_plain(p.get('description'))}",
             f"Materials: {_plain(p.get('materials_text'))}"]
    parts += [f"Step {i + 1}: {_plain(s.get('step'))}" for i, s in enumerate(ordered)]
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", p.get("doi") or "") or None
    return {"title": _plain(p.get("title")), "doi": doi, "url": p.get("url") or f"https://www.protocols.io/view/{p.get('uri')}",
            "text": "\n\n".join(parts)}


if __name__ == "__main__":  # python -m sdc.pio: link parsing offline
    assert ref("https://www.protocols.io/view/colony-pcr-gzbbx2n") == "colony-pcr-gzbbx2n"
    assert ref("dx.doi.org/10.17504/protocols.io.7cwhixe") == "10.17504%2Fprotocols.io.7cwhixe"
    assert ref("https://doi.org/10.17504/protocols.io.81wgbrknnlpk/v2") == "10.17504%2Fprotocols.io.81wgbrknnlpk%2Fv2"
    for bad in ("https://evil.example/x", "../../etc", "file:///x"):
        try:
            ref(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    print("pio self-check OK")
