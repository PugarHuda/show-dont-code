"""Score predicted steps against BioVL2 ground-truth step annotations (sentence,start,end)."""
import csv
import re
from difflib import SequenceMatcher


def load_gt(path):
    with open(path, encoding="utf-8") as f:
        return [{"sentence": r["sentence"], "start": float(r["start"]), "end": float(r["end"])} for r in csv.DictReader(f)]


def tiou(a, b):
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return inter / union if union > 0 else 0.0


# ponytail: hand-written synonym list for BioVL2's reagents; swap for an LLM judge if the vocabulary grows
SYNONYMS = [(r"dna polymerase|master ?mix|premix|prime ?star( max)?", "premix"),
            (r"distilled water|\bdw\b|nuclease.free water|milli.?q|\bmq\b", "water"),
            # sense/antisense = forward/reverse; BioVL2 numbers them Primer1/Primer2
            (r"primer ?1|forward primer|fwd primer|\bse primer|sense primer|\bf primer", "primer1"),
            (r"primer ?2|reverse primer|rev primer|\bas primer|antisense primer|\br primer", "primer2"),
            (r"template dna", "template")]


def _norm(s):
    s = (s or "").lower()
    for pat, canon in SYNONYMS:
        s = re.sub(pat, canon, s)
    return re.sub(r"[^a-z0-9 ]", " ", s).split()


def text_sim(gt_sentence, pred):
    """Best of: fuzzy match to the description, or the GT's key word (e.g. 'Primer1') appearing in the prediction."""
    g = _norm(gt_sentence)
    p = _norm(" ".join(str(pred.get(k) or "") for k in ("description", "reagent", "source")))
    fuzzy = SequenceMatcher(None, " ".join(g), " ".join(_norm(pred.get("description")))).ratio()
    content = [w for w in g if w not in {"add", "the", "a", "to", "of", "in", "into", "set"}]
    overlap = sum(w in p for w in content) / len(content) if content else 0.0
    return max(fuzzy, overlap)


def group(pred):
    """Merge back-to-back transfers of the same reagent into one protocol step ('Add Primer1' to 4 tubes = 1 step).

    BioVL2 annotates per reagent; the robot needs per-tube transfers. Scoring happens at the protocol level.
    """
    out = []
    for p in sorted(pred, key=lambda p: p["start"]):
        prev = out[-1] if out else None
        key = (p.get("action"), (p.get("reagent") or "").lower(), (p.get("source") or "").lower())
        if prev and key[1] and key == (prev.get("action"), (prev.get("reagent") or "").lower(), (prev.get("source") or "").lower()):
            prev["end"] = max(prev["end"], p["end"])
            prev["n"] = prev.get("n", 1) + 1
        else:
            out.append(dict(p))
    return out


def score(gt, pred, min_tiou=0.1, min_text=0.5):
    """Greedy one-to-one matching on grouped steps. A match needs temporal overlap AND the right content."""
    pred = group(pred)
    used, matches = set(), []
    for gi, g in enumerate(gt):
        best = None
        for pi, p in enumerate(pred):
            if pi in used:
                continue
            t, s = tiou(g, p), text_sim(g["sentence"], p)
            if t >= min_tiou and s >= min_text and (best is None or t + s > best[0]):
                best = (t + s, pi, t)
        if best:
            used.add(best[1])
            matches.append((gi, best[1], best[2]))
    order = [pred[pi]["start"] for _, pi, _ in matches]  # GT order vs predicted timing
    return {
        "gt_steps": len(gt),
        "pred_steps": len(pred),
        "recall": len(matches) / len(gt) if gt else 0.0,
        "precision": len(matches) / len(pred) if pred else 0.0,
        "mean_tiou": sum(t for *_, t in matches) / len(matches) if matches else 0.0,
        "order_correct": order == sorted(order),
        "matches": [{"gt": gt[g]["sentence"], "pred": pred[p]["description"], "tiou": round(t, 2)} for g, p, t in matches],
        "gt": gt,
    }


if __name__ == "__main__":
    gt = [{"sentence": "Add Primer1", "start": 64, "end": 99}, {"sentence": "Add Template", "start": 149, "end": 173}]
    pred = [
        {"start": 60, "end": 95, "description": "Pipette primer 1 into the PCR tube", "reagent": "Primer1"},
        {"start": 150, "end": 170, "description": "Add template DNA", "reagent": "template"},
        {"start": 300, "end": 310, "description": "Throw away tip", "reagent": None},
    ]
    r = score(gt, pred)
    assert r["recall"] == 1.0 and abs(r["precision"] - 2 / 3) < 1e-9 and r["order_correct"], r
    assert score(gt, list(reversed(pred[:2])))["order_correct"] is True  # matching is by time+content, not list position
    assert score(gt, [{**pred[0], "start": 200, "end": 210}])["recall"] == 0.0  # right content, wrong time
    four = [{"start": 60 + 9 * i, "end": 66 + 9 * i, "action": "transfer", "description": "Add primer 1", "reagent": "Primer1"} for i in range(4)]
    g = score(gt, four)
    assert g["pred_steps"] == 1 and g["recall"] == 0.5 and g["precision"] == 1.0, g  # 4 tube fills = 1 protocol step
    print("evaluate self-check OK")
