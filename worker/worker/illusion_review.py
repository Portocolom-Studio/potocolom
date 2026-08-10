"""Keyboard-driven review server for illusion runs.

Why this exists. The stage blind sheets rate one VIEW per case, shuffled so the
two views of an image land far apart, and show the rater nothing but a code. You
therefore cannot judge whether a view reads as its target subject (the target is
hidden) or whether the pair works as an illusion (you never see both views).
Only "is this a decent picture", which is not the question.

This serves the right unit: both views of one image together, with their two
target subjects, scored 0-5 from the keyboard. What stays hidden is the Dream
mode and the seed, because those are what a comparison could be biased by. The
prompts are shown because judging prompt-match without them is impossible, and
knowing the pair cannot bias "did this work".

The stage is hidden too. It used to be printed above the images, which told the
rater "this is the final" before they looked, and window 2 compares stages.

Final-stage items additionally record the two complaints window 1 could not
measure: frame/desk/paper-edge artifacts, and whether colour was delivered and
agreed between the views. They are asked ONLY on final items; four judgments on
every item would trade score quality for answers nobody needs at SDS-end, and
window 1 already measured the score drifting down over a long session.

Stdlib only, binds loopback, resumable, and it never overwrites a rating file
in place: verdicts are appended and the last one for an id wins.

    python -m worker.illusion_review --root <runs> --out ratings-pairs.jsonl
    python -m worker.illusion_review --export-ratings raw.jsonl --out canonical.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

STAGES = ("final", "dream_d1", "sds_end")
FRAME_LEVELS = ("none", "minor", "disqualifying")
YES_NO_NA = ("yes", "no", "na")


def questions(stage: str) -> list[str]:
    """Which fields this item asks for, in the order they are asked.

    The two colour questions used to be asked here and are not, because neither
    needs a human. derived_2 is derived_1 rotated 180 - the largest channel
    disagreement measured across window 2 is 1/255 - so the views cannot
    disagree about colour and the question can only be answered yes. And "is
    there colour at all" is mean chroma. Both are measured from the PNGs
    instead; the rows still carry the columns, filled with na by rating_row.
    """
    if stage != "final":
        return ["score"]
    return ["score", "frame_artifact"]


def _views_digest(views: list[Path]) -> str:
    digest = hashlib.sha256()
    for view in views:
        digest.update(view.read_bytes())
    return digest.hexdigest()


def stage_dir(run: Path, stage: str) -> Path | None:
    if stage == "final":
        return run
    if stage == "dream_d1":
        d = run / "ckpt_dream_round_01"
        return d if d.is_dir() else None
    sds = sorted(
        run.glob("ckpt_sds_[0-9][0-9][0-9][0-9]*"),
        key=lambda p: int(p.name.removeprefix("ckpt_sds_")),
    )
    return sds[-1] if sds else None


def collect(root: Path, stages: tuple[str, ...], seed: int) -> list[dict[str, Any]]:
    """One item per (run, stage), carrying both views and both target subjects.

    A forked base's arms each carry their own manifest, so every arm is its own
    item and the base contributes only the SDS-end state its arms share.
    """
    items: list[dict[str, Any]] = []
    for manifest_path in sorted(root.rglob("manifest.json")):
        try:
            m = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("status") != "completed":
            continue
        run = manifest_path.parent
        config = m.get("config") or {}
        subjects = m.get("subjects") or config.get("prompts") or ["?", "?"]
        style = m.get("style_requested") or config.get("style")
        seen_digests: set[str] = set()
        for stage in stages:
            d = stage_dir(run, stage)
            if d is None:
                continue
            views = [d / "derived_1.png", d / "derived_2.png"]
            if not all(v.is_file() for v in views):
                continue
            # With one Dream round, dream_d1 IS final. Rating the same two images
            # twice buys nothing and inflates the apparent sample size.
            digest = _views_digest(views)
            if digest in seen_digests:
                continue
            seen_digests.add(digest)
            # Opaque id: the reviewer must not be able to read mode or seed off it.
            ident = hashlib.sha256(f"{run}|{stage}|{seed}".encode()).hexdigest()[:12]
            items.append(
                {
                    "id": ident,
                    "stage": stage,
                    "subject_a": subjects[0],
                    "subject_b": subjects[1] if len(subjects) > 1 else "?",
                    "ask": questions(stage),
                    "paths": [str(v) for v in views],
                    "run_dir": str(run),
                    "pair_id": m.get("pair_id"),
                    "seed": config.get("seed"),
                    "mode": "joint" if config.get("dream_joint") else "indep",
                    "sds_steps": config.get("sds_steps"),
                    "arm": m.get("dream_arm") or "",
                    "spec_hash": m.get("spec_hash"),
                    "style": style,
                    "negative_prompt": config.get("negative_prompt"),
                }
            )
    random.Random(seed).shuffle(items)
    return items


def public_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the browser receives: enough to judge, and nothing else. The stage is
    not in here either - it used to be printed above the images, which primed the
    judgment window 2 needs unprimed."""
    return [{key: item[key] for key in ("id", "subject_a", "subject_b", "ask")} for item in items]


def rating_row(item: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """The recorded verdict: the score plus the complaint fields, with na for
    every question this item does not ask.

    Keyed for analysis by (spec_hash, stage, arm). spec_hash already covers
    prompts, style, seed, flags and models, so nothing that distinguishes two
    cells can be dropped by accident.
    """
    asked = set(item["ask"])
    score = int(answers["score"])
    if not 0 <= score <= 5:
        raise ValueError(f"score {score} out of range")
    frame = answers.get("frame_artifact") if "frame_artifact" in asked else None
    if frame is not None and frame not in FRAME_LEVELS:
        raise ValueError(f"frame_artifact must be one of {FRAME_LEVELS}")
    delivered = consistent = "na"
    if "colour_delivered" in asked:
        delivered = answers.get("colour_delivered", "na")
        if delivered not in YES_NO_NA:
            raise ValueError(f"colour_delivered must be one of {YES_NO_NA}")
        if delivered == "yes":
            # Only meaningful when there was colour to disagree about.
            consistent = answers.get("colour_consistent_between_views", "na")
            if consistent not in YES_NO_NA:
                raise ValueError(f"colour_consistent_between_views must be one of {YES_NO_NA}")
    return {
        "id": item["id"],
        "score": score,
        "frame_artifact": frame,
        "colour_delivered": delivered,
        "colour_consistent_between_views": consistent,
        "stage": item["stage"],
        "arm": item["arm"],
        "spec_hash": item["spec_hash"],
        "pair_id": item["pair_id"],
        "seed": item["seed"],
        "mode": item["mode"],
        "sds_steps": item["sds_steps"],
        "style": item["style"],
        "negative_prompt": item["negative_prompt"],
        "run_dir": item["run_dir"],
    }


def _run_identity(run_dir: str | None) -> dict[str, Any]:
    """spec_hash, arm and style from a run's manifest, for rating rows written
    before those fields were recorded."""
    if not run_dir:
        return {}
    try:
        manifest = json.loads((Path(run_dir) / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    config = manifest.get("config") or {}
    return {
        "spec_hash": manifest.get("spec_hash"),
        "arm": manifest.get("dream_arm") or "",
        "style": manifest.get("style_requested") or config.get("style"),
        "negative_prompt": config.get("negative_prompt"),
    }


def canonical_ratings(path: Path) -> list[dict[str, Any]]:
    """Last score per id wins, which is what the append-only log has always
    meant, plus the (spec_hash, stage, arm) analysis key on every row.

    Window 1's mode head-to-head came out 48/58/164 instead of 48/59/163 because
    an analysis keyed on fewer fields merged a 5k cell with a 10k one. Exporting
    the key with the row is how that stops recurring.
    """
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows[row["id"]] = row
    canonical: list[dict[str, Any]] = []
    for row in rows.values():
        merged = {**_run_identity(row.get("run_dir")), **row}
        merged["key"] = [merged.get("spec_hash"), merged.get("stage"), merged.get("arm") or ""]
        canonical.append(merged)
    return sorted(canonical, key=lambda row: (str(row["key"]), row["id"]))


def duplicate_keys(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Analysis keys claimed by more than one item, e.g. a re-attempted cell.
    Reported rather than merged: merging is the defect this exporter exists for.
    """
    seen: set[tuple[Any, ...]] = set()
    duplicates: list[list[Any]] = []
    for row in rows:
        key = tuple(row["key"])
        if key in seen:
            duplicates.append(list(key))
        seen.add(key)
    return duplicates


PAGE = """<!doctype html><meta charset=utf-8><title>Illusion review</title>
<style>
 :root{color-scheme:dark}
 body{font:15px/1.5 system-ui;background:#141414;color:#eee;margin:0;padding:1rem 1.25rem}
 header{display:flex;gap:1.5rem;align-items:baseline;flex-wrap:wrap}
 h1{font-size:1rem;margin:0;font-weight:600}
 .muted{color:#8b8b8b}
 .views{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:1rem 0;max-width:76rem}
 figure{margin:0}
 img{width:100%;border-radius:6px;background:#000;display:block}
 figcaption{margin-top:.4rem;color:#bbb}
 .scores{display:flex;gap:.5rem;flex-wrap:wrap;margin:.5rem 0 1rem}
 button{font:inherit;padding:.5rem .9rem;border-radius:6px;border:1px solid #3a3a3a;
   background:#1f1f1f;color:#eee;cursor:pointer}
 button:hover{background:#2b2b2b}
 button.on{background:#2f6f4f;border-color:#3f8f66}
 kbd{background:#222;border:1px solid #444;border-radius:4px;padding:0 .3rem;color:#ffd}
 #done{color:#7bd88f}
 .bar{height:4px;background:#242424;border-radius:2px;overflow:hidden;max-width:76rem}
 .bar div{height:100%;background:#3f8f66;width:0}
</style>
<header>
 <h1>Illusion review</h1>
 <span class=muted id=progress></span>
 <span class=muted><kbd>&larr;</kbd> back &middot; <kbd>s</kbd> skip</span>
</header>
<div class=bar><div id=fill></div></div>
<div class=views>
 <figure><img id=v1><figcaption>upright &mdash; should read as <b id=sa></b></figcaption></figure>
 <figure><img id=v2><figcaption>rotated 180 &mdash; should read as <b id=sb></b></figcaption></figure>
</div>
<p id=question></p>
<div class=scores id=scores></div>
<p class=muted>Stage, mode and seed are hidden on purpose. One key per answer;
 saved as you go.</p>
<script>
let items=[],rated={},i=0,answers={};
const $=id=>document.getElementById(id);
const KEYS={
  score:[['0',0],['1',1],['2',2],['3',3],['4',4],['5',5]],
  frame_artifact:[['c','none'],['m','minor'],['d','disqualifying']],
  colour_delivered:[['y','yes'],['n','no']],
  colour_consistent_between_views:[['y','yes'],['n','no']],
};
const LABEL={
  score:'score: 0 unusable, 3 both subjects readable, 5 would print it',
  frame_artifact:'frame, desk, hand or paper-edge artifact?',
  colour_delivered:'is there colour at all?',
  colour_consistent_between_views:'do both views agree on the colour?',
};
async function boot(){
  items=await (await fetch('/api/items')).json();
  rated=await (await fetch('/api/ratings')).json();
  i=items.findIndex(x=>!(x.id in rated)); if(i<0)i=items.length-1;
  show();
}
function pending(){
  const it=items[i]; if(!it)return null;
  for(const q of it.ask){
    if(answers[q]!==undefined)continue;
    if(q==='colour_consistent_between_views'&&answers.colour_delivered!=='yes')continue;
    return q;
  }
  return null;
}
function show(){
  const it=items[i];
  if(!it){$('progress').innerHTML='<span id=done>all done</span>';return}
  $('v1').src='/img?id='+it.id+'&view=1';
  $('v2').src='/img?id='+it.id+'&view=2';
  $('sa').textContent=it.subject_a; $('sb').textContent=it.subject_b;
  const n=Object.keys(rated).length;
  $('progress').textContent=`${i+1} / ${items.length}  (${n} rated)`;
  $('fill').style.width=(100*n/items.length)+'%';
  const q=pending();
  $('question').textContent=q?LABEL[q]:'answered - s for the next item';
  $('scores').replaceChildren();
  for(const [key,value] of (q?KEYS[q]:[])){
    const b=document.createElement('button');
    b.textContent=key+'  '+value;
    b.onclick=()=>answer(q,value);
    if(rated[it.id]&&rated[it.id][q]===value)b.classList.add('on');
    $('scores').append(b);
  }
}
async function answer(q,value){
  const it=items[i]; if(!it||!q)return;
  answers[q]=value;
  rated[it.id]={...(rated[it.id]||{}),...answers};
  await fetch('/api/rate',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({id:it.id,answers:answers})});
  if(pending()===null)next(); else show();
}
function next(){ if(i<items.length-1)i++; answers={}; show(); }
addEventListener('keydown',e=>{
  if(e.key==='ArrowLeft'){if(i>0){i--;answers={}}show();return}
  if(e.key==='ArrowRight'||e.key==='s'){next();return}
  const q=pending(); if(!q)return;
  const hit=KEYS[q].find(pair=>pair[0]===e.key);
  if(hit)answer(q,hit[1]);
});
boot();
</script>
"""


def serve(items: list[dict[str, Any]], out: Path, port: int) -> None:
    by_id = {it["id"]: it for it in items}
    public = public_items(items)
    out.parent.mkdir(parents=True, exist_ok=True)

    def existing() -> dict[str, Any]:
        if not out.is_file():
            return {}
        rows: dict[str, Any] = {}
        for line in out.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows[row["id"]] = row  # append-only; last wins
        return rows

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:  # quiet
            pass

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/items":
                self._send(200, json.dumps(public).encode(), "application/json")
            elif url.path == "/api/ratings":
                self._send(200, json.dumps(existing()).encode(), "application/json")
            elif url.path == "/img":
                q = parse_qs(url.query)
                item = by_id.get((q.get("id") or [""])[0])
                view = (q.get("view") or ["1"])[0]
                if item is None or view not in ("1", "2"):
                    self._send(404, b"no", "text/plain")
                    return
                self._send(200, Path(item["paths"][int(view) - 1]).read_bytes(), "image/png")
            else:
                self._send(404, b"no", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/rate":
                self._send(404, b"no", "text/plain")
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            item = by_id.get(payload.get("id"))
            if item is None:
                self._send(400, b"unknown id", "text/plain")
                return
            try:
                row = rating_row(item, payload.get("answers") or {})
            except (KeyError, TypeError, ValueError) as error:
                self._send(400, f"bad answers: {error}".encode(), "text/plain")
                return
            with out.open("a") as handle:
                handle.write(json.dumps(row) + "\n")
            self._send(200, b"ok", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"{len(items)} items over stages {sorted({i['stage'] for i in items})}")
    print(f"already rated: {len(existing())}")
    print(f"open http://127.0.0.1:{port}/   (Ctrl-C to stop; progress is saved as you go)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nstopped. {len(existing())} rated -> {out}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, help="campaign runs directory")
    ap.add_argument("--out", type=Path, required=True, help="JSONL to append verdicts to")
    ap.add_argument(
        "--export-ratings",
        type=Path,
        default=None,
        help="canonicalize a raw ratings JSONL (last score per id wins) to --out "
        "instead of serving, with the (spec_hash, stage, arm) key on every row",
    )
    ap.add_argument("--stages", default="final,dream_d1,sds_end")
    ap.add_argument("--seed", type=int, default=0, help="shuffle seed; also salts the ids")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    if args.export_ratings is not None:
        rows = canonical_ratings(args.export_ratings)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("".join(json.dumps(row) + "\n" for row in rows))
        print(json.dumps({"rows": len(rows), "duplicate_keys": duplicate_keys(rows)}, indent=2))
        return 0
    if args.root is None:
        ap.error("--root is required unless --export-ratings is given")

    stages = tuple(s.strip() for s in args.stages.split(",") if s.strip())
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        ap.error(f"unknown stage(s) {unknown}; choose from {list(STAGES)}")
    items = collect(args.root, stages, args.seed)
    if not items:
        ap.error(f"no completed runs with those stages under {args.root}")
    serve(items, args.out, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
