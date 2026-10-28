Upstream: https://github.com/speridlabs/iris-3b
Commit: 13fcf6a4f7fcb36561eac3daa5ad30d0b0a1822e

Vendored inference files:
- __init__.py
- config.py
- flow/__init__.py
- flow/schedule.py
- flow/solver.py
- flow/timesteps.py
- flow/transport.py
- models/__init__.py
- models/ac.py
- models/blocks/__init__.py
- models/blocks/mmdit.py
- models/blocks/pit.py
- models/blocks/single_stream.py
- models/dit.py
- models/presets.py
- nn/__init__.py
- nn/attention.py
- nn/embeddings.py
- nn/mlp.py
- nn/modulation.py
- nn/norms.py
- nn/rope.py
- registry.py
- sampling.py
- text/__init__.py
- text/base.py
- text/qwen3_vl.py

LICENSE and NOTICE are copied from the same upstream commit. Training-only repa.py and seeding.py are omitted.

Local change: `text/qwen3_vl.py` splits one Transformers import into two
equivalent imports to avoid a secret-scan false match. All other vendored
inference code is unchanged.
