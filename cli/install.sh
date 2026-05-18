#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# install.sh — Symlink the `oc` CLI into the user's PATH (issue #21).
#
# Usage: ./cli/install.sh
#
# Installs to ~/.local/bin/oc by default; override with OC_INSTALL_DIR.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${OC_INSTALL_DIR:-$HOME/.local/bin}"
TARGET="$INSTALL_DIR/oc"

mkdir -p "$INSTALL_DIR"
chmod +x "$SCRIPT_DIR/oc.py"
ln -sf "$SCRIPT_DIR/oc.py" "$TARGET"

echo "✓ Installed: $TARGET → $SCRIPT_DIR/oc.py"
echo
case ":$PATH:" in
  *":$INSTALL_DIR:"*) echo "✓ $INSTALL_DIR is on PATH";;
  *) echo "⚠ Add $INSTALL_DIR to your PATH:"
     echo "   echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
     ;;
esac
echo
echo "Try it: oc version  &&  oc list"
