#!/usr/bin/env python3
"""Static checks for the Vortex theme, run by .github/workflows/check.yml.

1. manifest.json / versions.json agree (the release workflow tags with the
   manifest version as-is, so it must be plain x.y.z).
2. theme.css parses: no CSS syntax errors, unbalanced braces or unclosed
   comments.
3. Obsidian theme guidelines: no !important, no @import, no remote assets.
4. The Style Settings @settings block is valid YAML with well-formed
   settings, and every setting is actually used by theme.css.
5. Every Vortex / palette variable (--vx-*, --ctp-*, --rp-*) that is
   referenced is also defined, which catches typos.
"""

import json
import re
import sys
from pathlib import Path

import tinycss2
import yaml

ROOT = Path(__file__).resolve().parents[2]
errors = []


def fail(message):
    errors.append(message)


# 1. Manifest -----------------------------------------------------------------

manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
versions = json.loads((ROOT / "versions.json").read_text(encoding="utf-8"))

for key in ("name", "version", "minAppVersion", "author"):
    if not manifest.get(key):
        fail(f'manifest.json: missing "{key}"')

version = manifest.get("version", "")
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    fail(f'manifest.json: version "{version}" must be x.y.z with no "v" prefix')
if versions.get(version) != manifest.get("minAppVersion"):
    fail(
        f'versions.json: "{version}" must map to the manifest minAppVersion '
        f'"{manifest.get("minAppVersion")}" (found {versions.get(version)!r})'
    )

# 2. CSS syntax ----------------------------------------------------------------

css = (ROOT / "theme.css").read_text(encoding="utf-8")

without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
if "/*" in without_comments:
    fail("theme.css: unclosed /* comment")

without_strings = re.sub(r'"(?:\\.|[^"\\])*"|' + r"'(?:\\.|[^'\\])*'", '""', without_comments)
depth = 0
for line_number, line in enumerate(without_strings.splitlines(), 1):
    for char in line:
        depth += char == "{"
        depth -= char == "}"
        if depth < 0:
            fail(f"theme.css:{line_number}: unexpected }}")
            depth = 0
if depth:
    fail(f"theme.css: {depth} unclosed {{ block(s)")


def parse_contents(content):
    if hasattr(tinycss2, "parse_blocks_contents"):
        return tinycss2.parse_blocks_contents(content, skip_comments=True, skip_whitespace=True)
    return tinycss2.parse_declaration_list(content, skip_comments=True, skip_whitespace=True)


def walk_tokens(tokens, line):
    for token in tokens:
        if token.type == "error":
            fail(f"theme.css:{token.source_line}: {token.message}")
        elif token.type == "url" and re.match(r"(https?:)?//", token.value):
            fail(f"theme.css:{token.source_line}: remote asset {token.value} (themes must work offline)")
        elif token.type == "function":
            if token.lower_name == "url":
                for arg in token.arguments:
                    if arg.type == "string" and re.match(r"(https?:)?//", arg.value):
                        fail(f"theme.css:{token.source_line}: remote asset {arg.value} (themes must work offline)")
            walk_tokens(token.arguments, line)
        elif token.type in ("() block", "[] block", "{} block"):
            walk_tokens(token.content, line)


def walk(nodes):
    for node in nodes:
        if node.type == "error":
            fail(f"theme.css:{node.source_line}: {node.message}")
        elif node.type == "at-rule":
            if node.lower_at_keyword == "import":
                fail(f"theme.css:{node.source_line}: @import is not allowed (themes must be a single local file)")
            walk_tokens(node.prelude, node.source_line)
            if node.content is not None:
                walk(parse_contents(node.content))
        elif node.type == "qualified-rule":
            walk_tokens(node.prelude, node.source_line)
            walk(parse_contents(node.content))
        elif node.type == "declaration":
            if node.important:
                fail(f"theme.css:{node.source_line}: !important on {node.name} (Obsidian's theme guidelines ask themes not to use it)")
            walk_tokens(node.value, node.source_line)


walk(tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True))

# 3. Style Settings -----------------------------------------------------------

defined = set(re.findall(r"(--[\w-]+)\s*:", without_comments))
used = set(re.findall(r"var\(\s*(--[\w-]+)", without_comments))

blocks = re.findall(r"/\*!?\s*@settings(.*?)\*/", css, flags=re.S)
if not blocks:
    fail("theme.css: no Style Settings @settings block found")

COLOR_FORMATS = {"hex", "rgb", "rgb-values", "rgb-split", "hsl", "hsl-values", "hsl-split", "hsl-split-decimal"}
VARIABLE_TYPES = {
    "variable-text", "variable-number", "variable-number-slider", "variable-select",
    "variable-color", "variable-themed-color",
}
TYPES = VARIABLE_TYPES | {"heading", "info-text", "class-toggle", "class-select", "color-gradient"}

for block in blocks:
    try:
        config = yaml.safe_load(block)
    except yaml.YAMLError as error:
        fail(f"@settings: invalid YAML: {error}")
        continue
    if not isinstance(config, dict):
        fail("@settings: block must be a mapping with name, id and settings")
        continue
    for key in ("name", "id", "settings"):
        if not config.get(key):
            fail(f'@settings: missing "{key}"')
    seen = set()
    for index, setting in enumerate(config.get("settings") or []):
        where = f"@settings[{index}]"
        if not isinstance(setting, dict):
            fail(f"{where}: must be a mapping")
            continue
        sid, stype = setting.get("id"), setting.get("type")
        where = f"@settings {sid or index}"
        if not sid:
            fail(f"{where}: missing id")
        elif sid in seen:
            fail(f"{where}: duplicate id")
        seen.add(sid)
        if not setting.get("title"):
            fail(f"{where}: missing title")
        if stype not in TYPES:
            fail(f"{where}: unknown type {stype!r}")
            continue
        if stype == "heading" and setting.get("level") not in range(1, 7):
            fail(f"{where}: heading level must be 1-6")
        if stype in VARIABLE_TYPES - {"variable-themed-color"} and "default" not in setting:
            fail(f"{where}: {stype} needs a default")
        if stype in ("variable-color", "variable-themed-color"):
            if setting.get("format") not in COLOR_FORMATS:
                fail(f"{where}: format must be one of {sorted(COLOR_FORMATS)}")
            for key in ("default",) if stype == "variable-color" else ("default-light", "default-dark"):
                value = str(setting.get(key, ""))
                if not re.fullmatch(r"#[0-9a-fA-F]{3,8}|rgba?\(.*\)", value):
                    fail(f"{where}: {key} must be a quoted hex or rgb() colour")
        if stype == "variable-number-slider":
            try:
                low, high, step = (float(setting[k]) for k in ("min", "max", "step"))
                if not (low <= float(setting["default"]) <= high) or step <= 0:
                    fail(f"{where}: default must sit between min and max, and step must be positive")
            except (KeyError, TypeError, ValueError):
                fail(f"{where}: variable-number-slider needs numeric min, max and step")
        if stype in ("class-select", "variable-select"):
            options = setting.get("options") or []
            values = [o.get("value") if isinstance(o, dict) else o for o in options]
            if not values:
                fail(f"{where}: needs options")
            elif not setting.get("allowEmpty") and setting.get("default") not in values:
                fail(f"{where}: default must be one of the options")
        if stype in VARIABLE_TYPES and sid and f"--{sid}" not in used:
            fail(f"{where}: theme.css never reads var(--{sid}), so this setting does nothing")
        if stype == "class-toggle" and sid and not re.search(r"\." + re.escape(sid) + r"(?![\w-])", without_comments):
            fail(f"{where}: theme.css has no .{sid} rule, so this toggle does nothing")

# 4. Undefined variables -------------------------------------------------------

for name in sorted(used):
    if re.match(r"--(vx|ctp|rp)-", name) and name not in defined:
        fail(f"theme.css: var({name}) is used but never defined")

# -----------------------------------------------------------------------------

if errors:
    print(f"{len(errors)} problem(s) found:")
    for message in errors:
        print(f"  - {message}")
    sys.exit(1)

settings_count = sum(len((yaml.safe_load(b) or {}).get("settings") or []) for b in blocks)
print(f"OK: manifest {version}, theme.css parses cleanly, {settings_count} Style Settings entries")
