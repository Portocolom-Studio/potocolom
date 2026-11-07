#!/usr/bin/env python3
"""Regenerate the worker's pinned Hub metadata, or its manifest history.
Run with worker/.venv/bin/python.

  generate-hub-lock.py                  rewrite worker/worker/hub_lock.json (needs the Hub)
  generate-hub-lock.py --history        add the current worker/models/*.json to
                                        worker/worker/manifest_history.json
  generate-hub-lock.py --history --from-git
                                        also add every version git holds of those files
                                        (once, from a full clone)
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker'))

from worker.manifests import (Manifest, load_manifests,  # noqa: E402
                              validate_capability_exclusivity, validate_revision_pins)
from worker.model_store import hub_file_text, hub_listing, repo_refs, upscale_targets, url_size  # noqa: E402

BUNDLED = ROOT / 'worker/models'
HISTORY = ROOT / 'worker/worker/manifest_history.json'
LOCK = ROOT / 'worker/worker/hub_lock.json'
DEVICES = ('cuda', 'rocm', 'cpu', 'mps')


def git_blobs() -> list[bytes]:
    """Every distinct version of worker/models/*.json in the history of HEAD."""
    git = ['git', '-C', str(ROOT)]
    commits = subprocess.run(git + ['log', '--format=%H', '--', 'worker/models'],
                             capture_output=True, text=True, check=True).stdout.split()
    objects = set()
    for commit in commits:
        listing = subprocess.run(git + ['ls-tree', '-r', '--format=%(objectname) %(path)',
                                        commit, '--', 'worker/models'],
                                 capture_output=True, text=True, check=True).stdout
        for line in listing.splitlines():
            oid, path = line.split(' ', 1)
            if path.startswith('worker/models/') and path.endswith('.json') and '/' not in path[14:]:
                objects.add(oid)
    return [subprocess.run(git + ['cat-file', 'blob', oid], capture_output=True, check=True).stdout
            for oid in sorted(objects)]


def history_entry(raw: bytes) -> dict:
    text = raw.decode('utf-8')
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    manifest_id = parsed.get('id') if isinstance(parsed, dict) else None
    return {'id': manifest_id, 'body': text}


def build_history(from_git: bool, bundled: Path = BUNDLED, history_file: Path = HISTORY) -> dict:
    """Existing entries are kept; every file given is added by its sha256."""
    history = json.loads(history_file.read_text()) if history_file.exists() else {}
    raws = git_blobs() if from_git else []
    raws += [path.read_bytes() for path in sorted(bundled.glob('*.json'))]
    for raw in raws:
        digest = hashlib.sha256(raw).hexdigest()
        if digest not in history:
            history[digest] = history_entry(raw)
    return dict(sorted(history.items()))


def historical_manifests(history: dict, bundled_ids: set[str]) -> list[Manifest]:
    """Stored bodies of ids no longer bundled, that the worker could load. Their pins
    stay in the lock, so an offline install can still adopt them."""
    found = []
    for entry in history.values():
        if entry['id'] in bundled_ids:
            continue
        try:
            manifest = Manifest.model_validate_json(entry['body'])
            validate_capability_exclusivity(manifest)
            validate_revision_pins(manifest)
        except ValueError:
            continue
        found.append(manifest)
    return found


def lock_manifests(bundled: Path = BUNDLED, history_file: Path = HISTORY) -> list[Manifest]:
    current = load_manifests(str(bundled))
    history = json.loads(history_file.read_text())
    return current + historical_manifests(history, {manifest.id for manifest in current})


def lock_refs(manifests: list[Manifest]) -> set[tuple[str, str, str]]:
    return {(ref.repo, ref.revision, ref.kind) for manifest in manifests
            for device in DEVICES for ref in repo_refs(manifest, device)}


def write_history(from_git: bool) -> None:
    history = build_history(from_git)
    HISTORY.write_text(json.dumps(history, indent=2, sort_keys=True) + '\n')
    print(f'{len(history)} manifest versions in {HISTORY.relative_to(ROOT)}')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--history', action='store_true',
                        help='update manifest_history.json instead of the lock')
    parser.add_argument('--from-git', action='store_true',
                        help='with --history: add every version git holds')
    args = parser.parse_args()
    if args.from_git and not args.history:
        parser.error('--from-git needs --history')
    if args.history:
        write_history(args.from_git)
        return
    manifests = lock_manifests()
    repos = {}
    for repo, revision, kind in sorted(lock_refs(manifests)):
        key = f'{repo}@{revision}'
        if key not in repos:
            files, gated, blobs = hub_listing(repo, revision)
            repos[key] = {'files': files, 'gated': gated, 'blobs': blobs}
        if kind == 'pipeline':
            names = {name for name, _ in repos[key]['files']}
            repos[key]['model_index'] = (hub_file_text(repo, revision, 'model_index.json')
                                         if 'model_index.json' in names else None)
        if kind == 'modular_pipeline':
            names = {name for name, _ in repos[key]['files']}
            repos[key]['modular_model_index'] = (
                hub_file_text(repo, revision, 'modular_model_index.json')
                if 'modular_model_index.json' in names else None)
        print(key, flush=True)
    urls = sorted({url for manifest in manifests if 'upscale' in manifest.capabilities
                   for url, _ in upscale_targets(manifest, '/tmp')})
    sizes = {url: url_size(url) for url in urls}
    LOCK.write_text(json.dumps({'repos': repos, 'urls': sizes}, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
