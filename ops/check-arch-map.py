#!/usr/bin/env python3
"""ops/check-arch-map.py — drift check between the recorded architecture map
(docs/CALL-TREE.md) and the code it describes.

The call-tree document records HOW the pipeline hangs together: which daemons
aidstack starts, which module spawns which subprocess, which dispatcher module
imports which, which persona each stage dispatches, which hooks are wired.
Prose drifts silently — this script extracts the same facts mechanically from
the code and compares them against the fact block embedded in the document, so
the suite fails the day the code and the document stop agreeing (the failure
mode ARCHITECTURE.md fell into — see docs/CALL-TREE.md "Why this file exists").

Extracted facts (all deterministic, sorted, no line numbers — a refactor that
moves a call within its function must NOT count as drift):

  [entrypoints]      repo scripts referenced by ops/atlas/aidstack.sh
  [imports]          dispatcher-sibling imports per dispatcher/*.py (ast walk,
                     nested/lazy imports included)
  [spawn-sites]      subprocess.run/Popen/... and proc_reaper.spawn call sites
                     in dispatcher/, dispatcher/hooks/, bot/bot.py, keyed by
                     (file, enclosing function, spawned command token)
  [stage-personas]   STAGE_AGENT_MAP literal from dispatcher/stage_prompts.py
  [prompt-dispatch]  subagent_type = "..." occurrences inside each STAGE_PROMPTS
                     value — the ACTUAL dispatch, hardwired in prompt text
                     (STAGE_AGENT_MAP does not select the persona; comparing the
                     two sections is how their divergence stays visible)
  [hooks]            .claude/settings.json hook wiring
  [personas]         persona files on disk in .claude/agents/

Usage:
  ops/check-arch-map.py            print the current fact block to stdout
  ops/check-arch-map.py --check    diff facts against docs/CALL-TREE.md;
                                   exit 0 in sync, 1 on drift (prints a diff)
  ops/check-arch-map.py --update   rewrite the fact block inside the document

Unlike ops/refresh-vendored-templates.sh (a report generator that never fails),
--check is a gate: the document lives in this repo, so drift is always fixable
in the same commit that caused it — run --update and re-read the prose above
the block for sentences the fact change just falsified.
"""

import ast
import difflib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "CALL-TREE.md"
FACTS_BEGIN = "<!-- arch-facts:begin -->"
FACTS_END = "<!-- arch-facts:end -->"

SUBPROCESS_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}
SPAWN_MODULES = {"subprocess"}
REAPER_NAMES = {"proc_reaper", "_proc_reaper"}


def _python_files():
    """The audited Python surface: dispatcher/ (flat), its hooks/, bot/bot.py."""
    files = sorted((REPO_ROOT / "dispatcher").glob("*.py"))
    files += sorted((REPO_ROOT / "dispatcher" / "hooks").glob("*.py"))
    bot = REPO_ROOT / "bot" / "bot.py"
    if bot.exists():
        files.append(bot)
    return files


def _rel(path):
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ── [entrypoints] ──────────────────────────────────────────────────────────

def extract_entrypoints():
    text = (REPO_ROOT / "ops" / "atlas" / "aidstack.sh").read_text()
    found = set(re.findall(r"dispatcher/[a-z_]+\.py", text))
    if re.search(r"\bbot\.py\b", text):
        found.add("bot/bot.py")
    return sorted(found)


# ── [imports] ──────────────────────────────────────────────────────────────

def extract_dispatcher_imports():
    siblings = {p.stem for p in (REPO_ROOT / "dispatcher").glob("*.py")}
    result = {}
    for path in sorted((REPO_ROOT / "dispatcher").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        deps = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                deps.update(a.name for a in node.names if a.name in siblings)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module in siblings:
                    deps.add(node.module)
        deps.discard(path.stem)
        if deps:
            result[path.stem] = sorted(deps)
    return result


# ── [spawn-sites] ──────────────────────────────────────────────────────────

def _first_token(call):
    """One stable token naming WHAT a spawn call executes."""
    if not call.args:
        return "?"
    head = call.args[0]
    if isinstance(head, ast.List) and head.elts:
        return _elt_token(head.elts[0], rest=head.elts[1:])
    return _elt_token(head, rest=[])


def _elt_token(node, rest):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute) and node.attr == "executable":
        # [sys.executable, SOME_SCRIPT, ...] — the script constant is the
        # interesting part, keep it in the token.
        for nxt in rest[:1]:
            if isinstance(nxt, ast.Name):
                return "sys.executable+" + nxt.id
        return "sys.executable"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in {"str", "list"} and len(node.args) == 1 \
            and isinstance(node.args[0], ast.Name):
        return node.args[0].id
    if isinstance(node, ast.Starred) and isinstance(node.value, ast.Name):
        return "*" + node.value.id
    return ast.unparse(node)[:40]


class _SpawnVisitor(ast.NodeVisitor):
    def __init__(self):
        self.sites = set()
        self._func_stack = ["<module>"]

    def visit_FunctionDef(self, node):
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner, attr = func.value.id, func.attr
            if (owner in SPAWN_MODULES and attr in SUBPROCESS_ATTRS) \
                    or (owner in REAPER_NAMES and attr == "spawn"):
                self.sites.add((self._func_stack[-1], _first_token(node)))
        self.generic_visit(node)


def extract_spawn_sites():
    result = {}
    for path in _python_files():
        visitor = _SpawnVisitor()
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        if visitor.sites:
            result[_rel(path)] = sorted(visitor.sites)
    return result


# ── [stage-personas] / [prompt-dispatch] ───────────────────────────────────

def _module_dict_assign(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == name \
                and isinstance(node.value, ast.Dict):
            return node.value
    raise SystemExit(f"error: dict literal `{name}` not found in stage_prompts.py")


def _strings_in(node):
    return "\n".join(n.value for n in ast.walk(node)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str))


def extract_stage_maps():
    tree = ast.parse((REPO_ROOT / "dispatcher" / "stage_prompts.py").read_text())
    personas = ast.literal_eval(_module_dict_assign(tree, "STAGE_AGENT_MAP"))
    dispatch = {}
    prompts = _module_dict_assign(tree, "STAGE_PROMPTS")
    for key, value in zip(prompts.keys, prompts.values):
        stage = key.value if isinstance(key, ast.Constant) else ast.unparse(key)
        types = re.findall(r'subagent_type\s*=\s*"([a-z-]+)"', _strings_in(value))
        seen, ordered = set(), []
        for t in types:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
        dispatch[stage] = ordered
    return personas, dispatch


# ── [hooks] ────────────────────────────────────────────────────────────────

def extract_hooks():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    result = {}
    for event, matchers in sorted(settings.get("hooks", {}).items()):
        commands = []
        for matcher in matchers:
            for hook in matcher.get("hooks", []):
                cmd = hook.get("command", "").replace("$CLAUDE_PROJECT_DIR/", "")
                if hook.get("async"):
                    cmd += " (async)"
                commands.append(cmd)
        result[event] = sorted(commands)
    return result


# ── [personas] ─────────────────────────────────────────────────────────────

def extract_personas():
    return sorted(p.stem for p in (REPO_ROOT / ".claude" / "agents").glob("*.md")
                  if p.name != "README.md")


# ── Rendering / comparison ─────────────────────────────────────────────────

def render_facts():
    lines = ["# arch-facts v1 — generated by ops/check-arch-map.py --update; "
             "do not edit by hand"]

    lines += ["", "[entrypoints:ops/atlas/aidstack.sh]"]
    lines += extract_entrypoints()

    lines += ["", "[imports:dispatcher]"]
    for mod, deps in sorted(extract_dispatcher_imports().items()):
        lines.append(f"{mod}: {', '.join(deps)}")

    lines += ["", "[spawn-sites]"]
    for path, sites in sorted(extract_spawn_sites().items()):
        for func, token in sites:
            lines.append(f"{path} :: {func} :: {token}")

    personas, dispatch = extract_stage_maps()
    lines += ["", "[stage-personas:STAGE_AGENT_MAP]"]
    lines += [f"{stage}: {persona}" for stage, persona in sorted(personas.items())]

    lines += ["", "[prompt-dispatch:subagent_type-in-STAGE_PROMPTS]"]
    for stage, types in sorted(dispatch.items()):
        lines.append(f"{stage}: {', '.join(types) if types else '(none)'}")

    lines += ["", "[hooks:.claude/settings.json]"]
    for event, commands in extract_hooks().items():
        for cmd in commands:
            lines.append(f"{event}: {cmd}")

    lines += ["", "[personas:.claude/agents]"]
    lines.append(", ".join(extract_personas()))

    return "\n".join(lines) + "\n"


def _split_doc(text):
    begin = text.find(FACTS_BEGIN)
    end = text.find(FACTS_END)
    if begin == -1 or end == -1 or end < begin:
        raise SystemExit(f"error: {FACTS_BEGIN} / {FACTS_END} markers not found "
                         f"(or out of order) in {_rel(DOC_PATH)}")
    head = text[:begin + len(FACTS_BEGIN)]
    tail = text[end:]
    return head, text[begin + len(FACTS_BEGIN):end], tail


def _recorded_facts(between):
    m = re.search(r"```[^\n]*\n(.*?)```\s*$", between, re.DOTALL)
    if not m:
        raise SystemExit(f"error: no fenced code block between the arch-facts "
                         f"markers in {_rel(DOC_PATH)}")
    return m.group(1)


def cmd_check():
    current = render_facts()
    recorded = _recorded_facts(_split_doc(DOC_PATH.read_text())[1])
    if current == recorded:
        print(f"IN_SYNC: {_rel(DOC_PATH)} matches the code")
        return 0
    diff = difflib.unified_diff(
        recorded.splitlines(keepends=True), current.splitlines(keepends=True),
        fromfile=f"{_rel(DOC_PATH)} (recorded)", tofile="code (extracted)")
    sys.stdout.writelines(diff)
    print(f"\nDRIFT: run `ops/check-arch-map.py --update`, then re-read the "
          f"prose in {_rel(DOC_PATH)} — a fact changed under it")
    return 1


def cmd_update():
    head, _, tail = _split_doc(DOC_PATH.read_text())
    DOC_PATH.write_text(head + "\n```\n" + render_facts() + "```\n" + tail)
    print(f"updated: {_rel(DOC_PATH)}")
    return 0


def main(argv):
    if argv == ["--check"]:
        return cmd_check()
    if argv == ["--update"]:
        return cmd_update()
    if not argv:
        sys.stdout.write(render_facts())
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
