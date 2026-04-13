#!/usr/bin/env python3
import argparse, json, csv, random, re, os, shutil
from pathlib import Path
from collections import Counter
from tree_sitter import Parser
from tree_sitter_languages import get_language
from bs4 import BeautifulSoup   # pip install beautifulsoup4

# -------------------------
# Tree-sitter setup
# -------------------------
LANG = get_language("javascript")
PARSER = Parser()
PARSER.set_language(LANG)

# -------------------------
# Load rules
# -------------------------
def load_rules(explicit_path, implicit_path):
    with open(explicit_path) as f: explicit = json.load(f)
    with open(implicit_path) as f: implicit = json.load(f)
    return explicit, implicit

# -------------------------
# Explicit rule mutations
# -------------------------
def apply_explicit_mutations(obj_text, explicit, mode, prob):
    edits, mutated = [], obj_text
    for dict_name, dict_spec in explicit.get("dictionaries", {}).items():
        for field, spec in dict_spec.items():
            # Insert required fields if missing
            if spec.get("required") and field not in mutated:
                if random.random() <= prob:
                    default_spec = spec.get("default")
                    if isinstance(default_spec, dict):
                        default_val = default_spec.get("value", None)
                    else:
                        default_val = None
                    insertion = f"{field}: {default_val if default_val is not None else 0},"
                    mutated = mutated.replace("{", "{" + insertion, 1)
                    edits.append((f"insert {field}", None, default_val, "explicit"))

            # Enum replacement
            if isinstance(spec.get("type"), str) and "GPU" in spec.get("type"):
                enum_name = spec["type"]
                if enum_name in explicit.get("enums", {}):
                    values = explicit["enums"][enum_name]
                    for v in values:
                        if v in mutated and random.random() <= prob:
                            new_v = random.choice([x for x in values if x != v]) \
                                    if mode == "invalid" else v
                            mutated = mutated.replace(v, new_v, 1)
                            edits.append(("replace enum", v, new_v, "explicit"))
    return mutated, edits

# -------------------------
# Implicit rule mutations
# -------------------------
def apply_implicit_mutations(obj_text, implicit, mode, prob):
    edits, mutated = [], obj_text
    for op in implicit.get("ops", []):
        for rule in op.get("requires", []) + op.get("effects", []):
            kind = rule.get("kind")
            target = rule.get("target", "")
            field = target.split(".")[-1] if isinstance(target, str) else None

            # numeric rules
            if kind == "multiple_of" and field:
                val_re = re.compile(rf"\b{field}\s*:\s*(\d+)")
                for m in val_re.finditer(mutated):
                    if random.random() > prob: continue
                    val, mult, new_val = int(m.group(1)), rule.get("value", 1), int(m.group(1))
                    if mode == "invalid" and val % mult == 0: new_val = val + 1
                    elif mode == "valid" and val % mult != 0: new_val = val + (mult - val % mult)
                    if new_val != val:
                        mutated = mutated.replace(m.group(0), f"{field}: {new_val}", 1)
                        edits.append((f"{field} multiple_of {mult}", val, new_val, "implicit"))

            elif kind == "min_value" and field:
                val_re = re.compile(rf"\b{field}\s*:\s*(\d+)")
                for m in val_re.finditer(mutated):
                    if random.random() > prob: continue
                    val, minv, new_val = int(m.group(1)), rule.get("value", 0), int(m.group(1))
                    if mode == "invalid" and val >= minv: new_val = minv - 1
                    elif mode == "valid" and val < minv: new_val = minv
                    if new_val != val:
                        mutated = mutated.replace(m.group(0), f"{field}: {new_val}", 1)
                        edits.append((f"{field} min_value {minv}", val, new_val, "implicit"))

            # flag rules
            elif kind == "flags_include":
                flags = rule.get("flags") or [rule.get("value")] if "value" in rule else []
                for flag in flags:
                    if not flag: continue
                    if random.random() <= prob:
                        if mode == "valid" and flag not in mutated:
                            mutated = mutated.replace("{", "{ usage: " + flag + ",", 1)
                            edits.append(("flags_include", None, flag, "implicit"))
                        elif mode == "invalid" and flag in mutated:
                            mutated = mutated.replace(flag, "0", 1)
                            edits.append(("remove required flag", flag, "0", "implicit"))

            elif kind == "forbid_flag_pair":
                pair = rule.get("pair") or rule.get("value") or []
                if len(pair) == 2:
                    f1, f2 = pair
                    if random.random() <= prob:
                        if mode == "invalid" and f1 in mutated and f2 not in mutated:
                            mutated = mutated.replace(f1, f1 + " | " + f2, 1)
                            edits.append(("forbid_flag_pair", f1, f1 + "|" + f2, "implicit"))
                        elif mode == "valid" and f1 in mutated and f2 in mutated:
                            mutated = mutated.replace(" | " + f2, "", 1)
                            edits.append(("remove forbidden flag", f1 + "|" + f2, f1, "implicit"))
    return mutated, edits

# -------------------------
# Ordering rules
# -------------------------
def apply_ordering_rules(code, implicit, mode, prob):
    edits, mutated = [], code
    if mode == "invalid":
        for op in implicit.get("ops", []):
            for rule in op.get("requires", []):
                if rule.get("kind") == "order_requires" and random.random() <= prob:
                    pre, post = None, None
                    if "value" in rule and isinstance(rule["value"], str) and "→" in rule["value"]:
                        try:
                            pre, post = rule["value"].split("→")
                        except Exception: continue
                    elif "pre" in rule and "post" in rule:
                        pre, post = rule["pre"], rule["post"]
                    if not pre or not post: continue
                    pat = rf"({pre}\(.*\);\s*)(.*{post}\()"
                    new_code, n = re.subn(pat, r"\2\1", mutated, count=1)
                    if n > 0:
                        mutated = new_code
                        edits.append(("order_requires", pre, post, "implicit"))
    return mutated, edits

# -------------------------
# AST processor for JS
# -------------------------
def process_js(code, explicit, implicit, mode, prob, path, writer, counter):
    try:
        tree = PARSER.parse(code.encode("utf-8"))
    except Exception:
        return code, False
    root, mutated, offset, edits_total = tree.root_node, code, 0, []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type == "call_expression":
            args = node.child_by_field_name("arguments")
            if not args: continue
            for arg in args.children:
                if arg.type == "object":
                    start, end = arg.start_byte + offset, arg.end_byte + offset
                    obj_text = mutated[start:end]
                    new_obj, edits1 = apply_explicit_mutations(obj_text, explicit, mode, prob)
                    new_obj, edits2 = apply_implicit_mutations(new_obj, implicit, mode, prob)
                    edits = edits1 + edits2
                    if edits:
                        mutated = mutated[:start] + new_obj + mutated[end:]
                        offset += len(new_obj) - (end - start)
                        for rule, old, new, src in edits:
                            writer.writerow([str(path), rule, old, new, src])
                            counter[rule] += 1
                        edits_total.extend(edits)
    mutated2, edits2 = apply_ordering_rules(mutated, implicit, mode, prob)
    if edits2:
        mutated = mutated2
        for rule, old, new, src in edits2:
            writer.writerow([str(path), rule, old, new, src])
            counter[rule] += 1
        edits_total.extend(edits2)
    return mutated, bool(edits_total)

# -------------------------
# HTML processor
# -------------------------
def process_html(path, outpath, explicit, implicit, mode, prob, writer, counter, input_dir, output_dir):
    html = Path(path).read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    mutated_any = False
    for script in soup.find_all("script"):
        if script.get("src"):
            src_path = script["src"]
            if src_path.endswith(".spec.js"):
                new_src = src_path.replace("cts", "cts_mutated")
                if new_src != src_path:
                    script["src"] = new_src
                    mutated_any = True
            continue
        code = script.string or ""
        mutated_code, changed = process_js(code, explicit, implicit, mode, prob, path, writer, counter)
        if changed:
            script.string = mutated_code
            mutated_any = True
    if mutated_any:
        outpath.parent.mkdir(parents=True, exist_ok=True)
        outpath.write_text(str(soup), encoding="utf-8")
        return True
    return False

# -------------------------
# Process all CTS files
# -------------------------
def process_all_specs(input_dir, output_dir, explicit, implicit, mode, prob, writer, counter):
    for root, dirs, files in os.walk(input_dir):
        rel_root = os.path.relpath(root, input_dir)
        out_root = os.path.join(output_dir, rel_root)
        os.makedirs(out_root, exist_ok=True)
        for f in files:
            in_path, out_path = os.path.join(root, f), os.path.join(out_root, f)
            if f.endswith(".spec.js"):
                code = Path(in_path).read_text(encoding="utf-8")
                mutated, changed = process_js(code, explicit, implicit, mode, prob, in_path, writer, counter)
                Path(out_path).write_text(mutated if changed else code, encoding="utf-8")
            elif f.endswith(".html"):
                process_html(in_path, Path(out_path), explicit, implicit, mode, prob, writer, counter, input_dir, output_dir)
            else:
                shutil.copy2(in_path, out_path)

# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--explicit", required=True)
    ap.add_argument("--implicit", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--mode", choices=["valid","invalid"], default="valid")
    ap.add_argument("--scale", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    prob = max(0.0, min(1.0, args.scale / 100.0))
    explicit, implicit = load_rules(args.explicit, args.implicit)
    counter = Counter()

    with open(args.report, "w", newline="") as rf:
        writer = csv.writer(rf)
        writer.writerow(["file","rule","old_value","new_value","source"])

        input_path = Path(args.input)
        output_path = Path(args.output)

        if input_path.is_file():
            # Single file mode
            if input_path.suffix == ".js":
                code = input_path.read_text(encoding="utf-8")
                mutated, changed = process_js(code, explicit, implicit, args.mode, prob, input_path, writer, counter)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(mutated if changed else code, encoding="utf-8")
            elif input_path.suffix == ".html":
                process_html(input_path, output_path, explicit, implicit, args.mode, prob, writer, counter, str(input_path.parent), str(output_path.parent))
        else:
            # Directory mode
            process_all_specs(str(input_path), str(output_path), explicit, implicit, args.mode, prob, writer, counter)

    print(f"Mutations applied: {sum(counter.values())}")
    for rule, count in counter.most_common():
        print(f"  {rule}: {count}")

if __name__ == "__main__":
    main()

