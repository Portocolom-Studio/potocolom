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

Stdlib only, binds loopback, resumable, and it never overwrites a rating file
in place: verdicts are appended and the last one for an id wins.

    python -m worker.illusion_review --root <runs> --out ratings-pairs.jsonl
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
    """One item per (run, stage), carrying both views and both target subjects."""
    items: list[dict[str, Any]] = []
    for manifest_path in sorted(root.rglob("manifest.json")):
        try:
            m = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("status") != "completed":
            continue
        run = manifest_path.parent
        subjects = m.get("subjects") or m.get("config", {}).get("prompts") or ["?", "?"]
        for stage in stages:
            d = stage_dir(run, stage)
            if d is None:
                continue
            views = [d / "derived_1.png", d / "derived_2.png"]
            if not all(v.is_file() for v in views):
                continue
            # Opaque id: the reviewer must not be able to read mode or seed off it.
            ident = hashlib.sha256(f"{run}|{stage}|{seed}".encode()).hexdigest()[:12]
            items.append(
                {
                    "id": ident,
                    "stage": stage,
                    "subject_a": subjects[0],
                    "subject_b": subjects[1] if len(subjects) > 1 else "?",
                    "paths": [str(v) for v in views],
                    "run_dir": str(run),
                    "pair_id": m.get("pair_id"),
                    "seed": m.get("config", {}).get("seed"),
                    "mode": "joint" if m.get("config", {}).get("dream_joint") else "indep",
                    "sds_steps": m.get("config", {}).get("sds_steps"),
                }
            )
    random.Random(seed).shuffle(items)
    return items


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
 <span class=muted>stage <b id=stage></b></span>
 <span class=muted><kbd>0</kbd>-<kbd>5</kbd> score &middot; <kbd>&larr;</kbd> back
  &middot; <kbd>s</kbd> skip</span>
</header>
<div class=bar><div id=fill></div></div>
<div class=views>
 <figure><img id=v1><figcaption>upright &mdash; should read as <b id=sa></b></figcaption></figure>
 <figure><img id=v2><figcaption>rotated 180 &mdash; should read as <b id=sb></b></figcaption></figure>
</div>
<div class=scores id=scores></div>
<p class=muted>0 = unusable, 3 = both subjects readable, 5 = would print it.
 Mode and seed are hidden on purpose. Your answer is saved as you go.</p>
<script>
let items=[],rated={},i=0;
const $=id=>document.getElementById(id);
async function boot(){
  items=await (await fetch('/api/items')).json();
  rated=await (await fetch('/api/ratings')).json();
  i=items.findIndex(x=>!(x.id in rated)); if(i<0)i=items.length-1;
  for(let s=0;s<=5;s++){
    const b=document.createElement('button');
    b.textContent=s; b.onclick=()=>score(s); b.dataset.s=s; $('scores').append(b);
  }
  show();
}
function show(){
  const it=items[i];
  if(!it){$('progress').innerHTML='<span id=done>all done</span>';return}
  $('v1').src='/img?id='+it.id+'&view=1';
  $('v2').src='/img?id='+it.id+'&view=2';
  $('sa').textContent=it.subject_a; $('sb').textContent=it.subject_b;
  $('stage').textContent=it.stage;
  const n=Object.keys(rated).length;
  $('progress').textContent=`${i+1} / ${items.length}  (${n} scored)`;
  $('fill').style.width=(100*n/items.length)+'%';
  for(const b of $('scores').children)
    b.classList.toggle('on', rated[it.id]!==undefined && String(rated[it.id])===b.dataset.s);
}
async function score(s){
  const it=items[i]; if(!it)return;
  rated[it.id]=s;
  await fetch('/api/rate',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({id:it.id,score:s})});
  if(i<items.length-1)i++; show();
}
addEventListener('keydown',e=>{
  if(e.key>='0'&&e.key<='5')score(+e.key);
  else if(e.key==='ArrowLeft'){if(i>0)i--;show()}
  else if(e.key==='ArrowRight'||e.key==='s'){if(i<items.length-1)i++;show()}
});
boot();
</script>
"""


def serve(items: list[dict[str, Any]], out: Path, port: int) -> None:
    by_id = {it["id"]: it for it in items}
    # The reviewer never receives identity, only what is needed to judge.
    public = [{k: it[k] for k in ("id", "stage", "subject_a", "subject_b")} for it in items]
    out.parent.mkdir(parents=True, exist_ok=True)

    def existing() -> dict[str, Any]:
        if not out.is_file():
            return {}
        scores: dict[str, Any] = {}
        for line in out.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            scores[row["id"]] = row["score"]  # append-only; last wins
        return scores

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
            row = {
                "id": item["id"],
                "score": int(payload["score"]),
                "stage": item["stage"],
                "pair_id": item["pair_id"],
                "seed": item["seed"],
                "mode": item["mode"],
                "sds_steps": item["sds_steps"],
                "run_dir": item["run_dir"],
            }
            with out.open("a") as handle:
                handle.write(json.dumps(row) + "\n")
            self._send(200, b"ok", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"{len(items)} items over stages {sorted({i['stage'] for i in items})}")
    print(f"already scored: {len(existing())}")
    print(f"open http://127.0.0.1:{port}/   (Ctrl-C to stop; progress is saved as you go)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nstopped. {len(existing())} scored -> {out}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="campaign runs directory")
    ap.add_argument("--out", type=Path, required=True, help="JSONL to append verdicts to")
    ap.add_argument("--stages", default="final,dream_d1,sds_end")
    ap.add_argument("--seed", type=int, default=0, help="shuffle seed; also salts the ids")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

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
