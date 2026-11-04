#!/usr/bin/env python3
"""Regenerate the worker's pinned Hub metadata. Run with worker/.venv/bin/python."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker'))

from worker.manifests import load_manifests  # noqa: E402
from worker.model_store import hub_file_text, hub_listing, repo_refs, upscale_targets, url_size  # noqa: E402


def main():
    manifests = load_manifests(str(ROOT / 'worker/models'))
    refs = {(ref.repo, ref.revision, ref.kind) for manifest in manifests
            for device in ('cuda', 'rocm', 'cpu', 'mps') for ref in repo_refs(manifest, device)}
    repos = {}
    for repo, revision, kind in sorted(refs):
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
    (ROOT / 'worker/worker/hub_lock.json').write_text(
        json.dumps({'repos': repos, 'urls': sizes}, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
