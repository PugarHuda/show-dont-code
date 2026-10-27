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


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()


def text_sim(gt_sentence, pred):
    """Best of: fuzzy match to the description, or the GT's key word (e.g. 'Primer1') appearing in the prediction."""
    g = _norm(gt_sentence)
    p = _norm(" ".join(str(pred.get(k) or "") for k in ("description", "reagent", "source")))
    fuzzy = SequenceMatcher(None, " ".join(g), " ".join(_norm(pred.get("description")))).ratio()
    content = [w for w in g if w not in {"add", "the", "a", "to", "of", "in", "into", "set"}]
    overlap = sum(w in p for w in content) / len(content) if content else 0.0
    return max(fuzzy, overlap)


def score(gt, pred, min_tiou=0.1, min_text=0.5):
    """Greedy one-to-one matching. A match needs temporal overlap AND the right content."""
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
    print("evaluate self-check OK")
