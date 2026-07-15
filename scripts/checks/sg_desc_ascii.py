#!/usr/bin/env python3
"""sg_desc_ascii.py — flag non-ASCII in CFN-bound description= arguments.

EC2 SecurityGroup / ingress·egress rule GroupDescription only accepts ASCII;
an em-dash or CJK char makes CloudFront return 400 and rolls the whole stack
back (repeatedly hit: #239 and the 2026-07-14 Singapore rebuild). CfnOutput's
description does allow non-ASCII, so we must not flag those.

Uses the AST to check the description= keyword's enclosing call: report only
when the call target name looks security-group / rule related. Prints
"LINE: <src>" for each offending line; empty output = clean. Fails open
(prints nothing) on syntax errors so it never blocks unrelated work.
"""

import ast
import sys

# call names whose `description=` renders into a CFN GroupDescription
_SG_CALL_HINTS = (
    "SecurityGroup",
    "add_ingress_rule",
    "add_egress_rule",
    "IngressRule",
    "EgressRule",
    "SecurityGroupRule",
    "CfnSecurityGroup",
)


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def main() -> int:
    path = sys.argv[1]
    try:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return 0  # fail open — don't block on unparseable files
    lines = src.splitlines()
    flagged = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if not any(h in name for h in _SG_CALL_HINTS):
            continue
        for kw in node.keywords:
            if kw.arg != "description":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                if any(ord(c) > 127 for c in kw.value.value):
                    ln = kw.value.lineno
                    flagged.append(
                        (ln, lines[ln - 1].strip() if ln <= len(lines) else "")
                    )
    for ln, text in sorted(set(flagged)):
        print(f"{ln}: {text[:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
