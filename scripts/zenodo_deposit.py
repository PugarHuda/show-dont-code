"""Deposit a run's RO-Crate on Zenodo and get it a DOI: the crate (protocol.py, every model output, the receipt and its
Bitcoin timestamp, the robot-server report) becomes a citable dataset.

  ZENODO_TOKEN=... python scripts/zenodo_deposit.py pcr_1-gt                 # a draft, reserved DOI, nothing public
  ZENODO_TOKEN=... python scripts/zenodo_deposit.py pcr_1-gt --publish       # publish: permanent and public
  ZENODO_URL=https://sandbox.zenodo.org ...                                  # Zenodo's test instance (test DOIs)

The token needs the deposit:write (and deposit:actions to publish) scopes: zenodo.org/account/settings/applications.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

UA = "show-dont-code/1.0 (+https://github.com/PugarHuda/show-dont-code)"  # Cloudflare refuses Python's default (1010)
API = "https://pugarhuda--show-dont-code-web.us-east.modal.run"
ZENODO = os.getenv("ZENODO_URL", "https://zenodo.org").rstrip("/")


def _z(method, path, token, body=None, data=None, ctype="application/json"):
    url = path if path.startswith("http") else f"{ZENODO}/api{path}"
    payload = json.dumps(body).encode() if body is not None else data
    req = urllib.request.Request(url, data=payload, method=method,
                                 headers={"Authorization": f"Bearer {token}", "User-Agent": UA, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Zenodo said {e.code} to {method} {url}: {e.read().decode()[:500]}") from None


def metadata(run, rec):
    """Zenodo metadata from the run's receipt: what it is, where it came from, how to check it."""
    gt = "note" in rec["models"]
    return {
        "upload_type": "dataset",
        "title": f"Show, Don't Code run {run}: an Opentrons Flex protocol recovered from a first-person lab video",
        "creators": [{"name": "Mantoro, Pugar Huda", "affiliation": "Universitas Islam Indonesia"}],
        "description": (
            "<p>An RO-Crate (Process Run Crate 0.5) of one run of Show, Don't Code: a protocol filmed once by hand, turned "
            "into an Opentrons Flex protocol (protocol.py) by deterministic code generation"
            + (" from the BioVL2 human step annotations" if gt else " from the steps Meta's Muse Spark and SAM 3.1 found in the video")
            + ". Included: every model output, the scientist's answers, Opentrons' protocol analysis, the robot-server "
            "report of a run on Opentrons' robot software in Flex simulation, PyLabRobot translations, tube labels, and "
            "a reproducibility receipt with SHA-256 of every file, timestamped in Bitcoin with OpenTimestamps.</p>"
            f"<p>protocol.py SHA-256: <code>{rec['protocol_sha256']}</code></p>"
            "<p>The video itself is not included; it is part of BioVL2 (CC BY 4.0), cited below.</p>"),
        "access_right": "open",
        "license": "cc-by-4.0",
        "keywords": ["lab automation", "Opentrons", "Opentrons Flex", "RO-Crate", "PCR", "video understanding",
                     "reproducibility", "OpenTimestamps"],
        "related_identifiers": [
            {"identifier": "10.5281/zenodo.21495188", "relation": "isDerivedFrom", "scheme": "doi", "resource_type": "dataset"},
            {"identifier": "https://github.com/PugarHuda/show-dont-code", "relation": "isSupplementTo", "scheme": "url",
             "resource_type": "software"},
            {"identifier": f"https://show-dont-code.vercel.app/app?run={run}", "relation": "isDocumentedBy", "scheme": "url"}],
    }


def deposit(run, token, publish=False, api=API, log=print):
    with urllib.request.urlopen(f"{api}/api/runs/{run}/receipt.json", timeout=180) as r:
        rec = json.load(r)
    with urllib.request.urlopen(f"{api}/api/runs/{run}/ro-crate.zip", timeout=300) as r:
        crate = r.read()
    dep = _z("POST", "/deposit/depositions", token, {"metadata": {**metadata(run, rec), "prereserve_doi": True}})
    doi = dep["metadata"]["prereserve_doi"]["doi"]
    log(f"draft {dep['id']} created, DOI {doi} reserved")
    _z("PUT", f"{dep['links']['bucket']}/{run}-ro-crate.zip", token, data=crate, ctype="application/octet-stream")
    log(f"uploaded {run}-ro-crate.zip ({len(crate) >> 10} KB)")
    out = {"id": dep["id"], "doi": doi, "draft": dep["links"]["html"], "published": False}
    if publish:
        pub = _z("POST", f"/deposit/depositions/{dep['id']}/actions/publish", token)
        out.update(published=True, doi=pub["doi"], record=pub["links"]["record_html"])
        log(f"published: https://doi.org/{pub['doi']}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run")
    ap.add_argument("--publish", action="store_true", help="publish the record (permanent, public)")
    ap.add_argument("--api", default=API)
    a = ap.parse_args()
    token = os.getenv("ZENODO_TOKEN") or sys.exit("set ZENODO_TOKEN")
    print(json.dumps(deposit(a.run, token, a.publish, a.api), indent=1))


if __name__ == "__main__":
    main()
