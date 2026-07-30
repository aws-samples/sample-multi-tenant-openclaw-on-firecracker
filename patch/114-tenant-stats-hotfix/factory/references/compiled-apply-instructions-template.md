# Applying {{KIT_ID}}

This is a generated `{{LANE}}` patch kit. Open Claude Code in this directory so it
loads `CLAUDE.md`, then ask it to apply the patch. The executable contract is the
packaged `runtime/scripts/patch-set.sh`; the scripts under `lib/compiled/` are internal
lane steps and must not be called directly.

The safe order is:

1. read-only discovery into an `environment.json` outside this directory;
2. exact independent review-fingerprint check;
3. read-only plan and one batch of operator decisions;
4. fixed-driver apply;
5. independent live verify;
6. a real second fixed-driver run proving `SKIP` with zero writes.

For a multi-kit patch, invoke the first kit's set driver and list all sibling kits in
dependency order. The set stops at the first failure and records the partial state.
Read `CLAUDE.md` for the full command sequence, exit-code handling, and completion
evidence.
