# Claw Patch v2 Qualification Kit

This directory is a portable, `QUALIFY_ONLY` qualification artifact for the
publish commit recorded in `publish-provenance.json`. It is not bound to a
customer AWS account and cannot be approved for writes.

Validate the packaged kit without the source skill tree:

```bash
python3 handlers/patchctl.py validate \
  --manifest manifest.v2.json \
  --environment environment.compiled.json
```

Before customer execution, rerun `claw-patch-v2 from_publish` with the same
publish receipt, the reviewed `patch/monitor-patch/manifest.json` acceptance
manifest, and the real customer target profile. Then rerun `compile_patch` and
review the resulting plan. Do not edit `candidate-recipe.json` or
`environment.compiled.json` to retarget this kit; their hashes bind the
qualification profile.

This qualification compiles 5 resource operations and keeps 19 operations,
including 12 reviewed behavior oracles, as evidence-bound `MANUAL` work.
