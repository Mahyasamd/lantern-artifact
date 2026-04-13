# idl_extract_final.py
import subprocess, sys, json, re
from bs4 import BeautifulSoup
import pywebidl2
from pprint import pprint

# ---------------------------
# Helpers
# ---------------------------

def _idl_type_node(n):
    if not isinstance(n, dict):
        return n
    return n.get("idl_type") or n.get("idlType") or n

def _idl_type_to_str(t):
    t = _idl_type_node(t)
    if t is None:
        return "any"
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        if "idlType" in t and not t.get("type"):
            return _idl_type_to_str(t["idlType"])
        if t.get("type") == "union" or t.get("union"):
            parts = t.get("idlType", [])
            return " or ".join(_idl_type_to_str(p) for p in parts) if parts else "any"
        if "generic" in t:
            inner = _idl_type_to_str(t.get("idlType"))
            return f"{t['generic']}<{inner}>" if inner else t["generic"]
        if "name" in t:
            return str(t["name"])
        if isinstance(t.get("type"), str):
            return t["type"]
        return str(t)
    if isinstance(t, list):
        return " ".join(_idl_type_to_str(x) for x in t)
    return str(t)

def _arg_list_to_mutator_shape(arguments):
    out = []
    for a in (arguments or []):
        out.append({
            "name": a.get("name"),
            "type": _idl_type_to_str(_idl_type_node(a.get("idl_type") or a.get("idlType") or a))
        })
    return out

# Preprocess: make flags parseable and drop attrs pywebidl2 dislikes
def _preprocess_text_for_parser(text: str) -> str:
    s = re.sub(r'(\bconst\s+)GPUFlagsConstant\b', r'\1unsigned long', text)  # const GPUFlagsConstant -> unsigned long
    s = s.replace('[NewObject]', '')
    return s

# ---------------------------
# Regexes for flags/typedefs/mixins
# ---------------------------
FLAGS_NS_RE = re.compile(
    r'namespace\s+(GPU(?:BufferUsage|TextureUsage|MapMode|ShaderStage|ColorWrite))\s*{(?P<body>.*?)}\s*;',
    re.DOTALL
)

# capture NAME and VALUE (hex or decimal)
CONST_RE = re.compile(
    r'\bconst\s+\w+\s+([A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;',
    re.MULTILINE
)

TYPEDEF_RE = re.compile(
    r'\btypedef\s+(?:\[[^\]]*\]\s+)?([A-Za-z ]*long long|[A-Za-z ]*long|[A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*;',
    re.MULTILINE
)

MIXIN_BLOCK_RE = re.compile(r'(partial\s+)?interface\s+mixin\s+([A-Za-z_]\w*)\s*{(.*?)}\s*;', re.DOTALL)
OP_RE          = re.compile(r'(?:\[[^\]]*\]\s*)?([A-Za-z_][\w <>\-]*?)\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*;', re.DOTALL)
ARG_SPLIT_RE   = re.compile(r',(?![^\(<]*[\)>])')  # split top-level commas

def _parse_args_simple(arglist: str):
    arglist = arglist.strip()
    if not arglist or arglist == "void": return []
    args = []
    for raw in ARG_SPLIT_RE.split(arglist):
        token = raw.strip()
        if not token: continue
        token = re.sub(r'\[[^\]]*\]\s*', '', token)     # drop extended attrs
        token = re.sub(r'\s*=\s*[^,]+$', '', token)     # drop defaults
        token = re.sub(r'^\s*optional\s+', '', token)   # drop 'optional'
        parts = token.split()
        if len(parts) == 1:
            name = ""
            typ  = parts[0]
        else:
            name = parts[-1]
            typ  = " ".join(parts[:-1])
        args.append({"name": name, "type": typ})
    return args

def _is_flags_namespace_block(raw: str) -> bool:
    return bool(FLAGS_NS_RE.search(raw))

# ---------------------------
# 1) Bikeshed
# ---------------------------
print("Running Bikeshed to extract WebIDL...")
try:
    subprocess.run(["bikeshed", "spec", "spec/index.bs"], check=True)
    print("Bikeshed build succeeded.")
except subprocess.CalledProcessError:
    print("Bikeshed failed.")
    sys.exit(1)

# ---------------------------
# 2) Extract IDL blocks
# ---------------------------
print("Reading from index.html...")
with open("index.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f, "html.parser")

idl_blocks = soup.find_all("pre", class_="idl") or soup.find_all("pre", {"data-link-type": "idl"}) or [
    tag for tag in soup.find_all("pre")
    if "idl" in tag.get("class", []) or "idl" in tag.get("data-link-type", "")
]

if not idl_blocks:
    print("No IDL blocks found in index.html.")
    sys.exit(1)

print(f"IDL blocks extracted: {len(idl_blocks)}")

raw_blocks = [blk.get_text() for blk in idl_blocks]
full_raw_idl = "\n\n".join(raw_blocks)

with open("cleaned_webgpu.idl", "w", encoding="utf-8") as out_file:
    out_file.write(full_raw_idl)
print("Cleaned IDL dumped to cleaned_webgpu.idl")

# ---------------------------
# 3) Parse block-by-block (skip flags namespaces)
# ---------------------------
definitions = []
for raw in raw_blocks:
    if _is_flags_namespace_block(raw):
        continue
    block = _preprocess_text_for_parser(raw).strip()
    if not block:
        continue
    try:
        parsed = pywebidl2.parse(block)
        if isinstance(parsed, dict) and "definitions" in parsed:
            definitions.extend(parsed["definitions"])
        elif isinstance(parsed, list):
            definitions.extend(parsed)
    except Exception:
        # Quietly skip; mixin/flags fallbacks will fill gaps
        pass

# ---------------------------
# 4) Build base rules from parsed defs
# ---------------------------
rules = {
    "interfaces": {},
    "dictionaries": {},
    "enums": {},
    "typedefs": {},
    "mixins": {},
    "namespaces": {},  # we'll store numeric constants for flags here
    "callbacks": {},
    "includes": []
}

for item in definitions:
    if not isinstance(item, dict): 
        continue
    t = item.get("type")
    if not t:
        continue

    if t == "interface":
        iname = item.get("name")
        if not iname: continue
        iface = rules["interfaces"].setdefault(iname, {"methods": {}})
        for m in item.get("members", []) or []:
            if not isinstance(m, dict): continue
            if m.get("type") == "operation":
                mname = m.get("name")
                if not mname: continue
                iface["methods"][mname] = {"args": _arg_list_to_mutator_shape(m.get("arguments"))}

    elif t == "dictionary":
        dname = item.get("name")
        if not dname: continue
        props = rules["dictionaries"].setdefault(dname, {})
        for m in item.get("members", []) or []:
            if not isinstance(m, dict): continue
            pname = m.get("name")
            if not pname: continue
            props[pname] = {
                "type": _idl_type_to_str(_idl_type_node(m.get("idl_type") or m.get("idlType") or m)),
                "required": m.get("required"),
                "default": m.get("default"),
            }

    elif t == "enum":
        ename = item.get("name")
        if not ename: continue
        vals = []
        for v in item.get("values", []) or []:
            if isinstance(v, dict) and "value" in v: vals.append(v["value"])
            elif isinstance(v, str): vals.append(v)
        rules["enums"][ename] = vals

    elif t == "typedef":
        new_t = item.get("new_type")
        old_t = _idl_type_to_str(item.get("idl_type") or item.get("idlType"))
        if new_t:
            rules["typedefs"][new_t] = old_t

    elif t == "interface mixin":
        mname = item.get("name")
        if not mname: continue
        mix = rules["mixins"].setdefault(mname, {"members": []})
        for m in item.get("members", []) or []:
            if not isinstance(m, dict): continue
            mix["members"].append({
                "name": m.get("name"),
                "type": m.get("type"),
                "idl_type": _idl_type_to_str(m.get("idl_type") or m.get("idlType")),
                "arguments": _arg_list_to_mutator_shape(m.get("arguments"))
            })

    elif t == "namespace":
        # We skip flags namespaces in parsing, but keep any others if they sneak through
        nname = item.get("name")
        if not nname: continue
        ns = rules["namespaces"].setdefault(nname, {"members": []})
        for m in item.get("members", []) or []:
            if not isinstance(m, dict): continue
            member = {
                "name": m.get("name"),
                "type": m.get("type"),
                "idl_type": _idl_type_to_str(m.get("idl_type") or m.get("idlType")),
                "arguments": _arg_list_to_mutator_shape(m.get("arguments"))
            }
            if "value" in m:
                member["value"] = m["value"]
            ns["members"].append(member)

    elif t == "callback":
        cname = item.get("name")
        if not cname: continue
        rules["callbacks"][cname] = {
            "idl_type": _idl_type_to_str(item.get("idl_type") or item.get("idlType")),
            "arguments": _arg_list_to_mutator_shape(item.get("arguments"))
        }

    elif t == "includes":
        rules["includes"].append({"target": item.get("target"), "includes": item.get("includes")})

# ---------------------------
# 5) Extract flags + typedefs (with numeric values) from the full IDL text
# ---------------------------
flags_from_doc = {}
typedefs_from_doc = {}
for m in FLAGS_NS_RE.finditer(full_raw_idl):
    ns = m.group(1); body = m.group('body')
    pairs = CONST_RE.findall(body)  # [(NAME, VALUE), ...]
    if not pairs:
        continue
    names = []
    consts = []
    for name, value in pairs:
        names.append(name)
        consts.append({"name": name, "value": value})
    # Enums: list of names (for validation/mutation)
    flags_from_doc[ns] = list(dict.fromkeys(names))
    # Namespaces: keep numeric values too
    rules["namespaces"][ns] = {"kind": "flags", "constants": consts}

for tmatch in TYPEDEF_RE.finditer(full_raw_idl):
    old_t, new_t = tmatch.group(1).strip(), tmatch.group(2).strip()
    typedefs_from_doc[new_t] = old_t

# Merge flags/enums and typedefs
for ns, vals in flags_from_doc.items():
    current = rules["enums"].get(ns, [])
    rules["enums"][ns] = list(dict.fromkeys((current or []) + vals))
for alias, src in typedefs_from_doc.items():
    rules["typedefs"][alias] = src

# ---------------------------
# 6) Fallback: parse mixin operations from full IDL (ensures draw/setBindGroup/… exist)
# ---------------------------
fallback_mixins = {}
for mm in MIXIN_BLOCK_RE.finditer(full_raw_idl):
    mixin_name, body = mm.group(2), mm.group(3)
    for opm in OP_RE.finditer(body):
        ret_type, op_name, arglist = opm.group(1).strip(), opm.group(2).strip(), opm.group(3)
        # Ignore obvious non-ops
        if op_name in ("attribute",):
            continue
        args = _parse_args_simple(arglist)
        fallback_mixins.setdefault(mixin_name, []).append({
            "name": op_name,
            "type": "operation",
            "idl_type": ret_type,
            "arguments": args
        })

# Merge fallback mixins into rules["mixins"]
for mixin_name, members in fallback_mixins.items():
    mix = rules["mixins"].setdefault(mixin_name, {"members": []})
    have = {m.get("name") for m in mix["members"] if m.get("name")}
    for m in members:
        if m["name"] not in have:
            mix["members"].append(m)

# ---------------------------
# 7) Apply includes (merge mixin ops into interfaces)
# ---------------------------
for inc in rules.get("includes", []):
    target = inc.get("target")
    mixin  = inc.get("includes")
    if not target or not mixin: continue
    members = rules.get("mixins", {}).get(mixin, {}).get("members", [])
    if not members: continue
    rules["interfaces"].setdefault(target, {"methods": {}})
    methods = rules["interfaces"][target]["methods"]
    for m in members:
        if m.get("type") == "operation" and m.get("name"):
            if m["name"] not in methods:
                methods[m["name"]] = {
                    "args": [{"name": a.get("name"), "type": _idl_type_to_str(_idl_type_node(a))}
                             for a in (m.get("arguments") or [])]
                }

# ---------------------------
# 8) Output
# ---------------------------
print("\nPretty-printed WebGPU Explicit Rules (mutator shape)")
pprint(rules); print("End of Rules")

with open("webgpu_explicit_rules.json", "w", encoding="utf-8") as f:
    json.dump(rules, f, indent=2)
print("JSON output saved to 'webgpu_explicit_rules.json'")

print("\nSuccessfully extracted:")
print(f"  - {len(rules['interfaces'])} interfaces")
print(f"  - {sum(len(v['methods']) for v in rules['interfaces'].values())} interface methods")
print(f"  - {len(rules['dictionaries'])} dictionaries")
print(f"  - {len(rules['enums'])} enums")
print(f"  - {len(rules['typedefs'])} typedefs")
print(f"  - {len(rules['mixins'])} mixins")
print(f"  - {len(rules['namespaces'])} namespaces")  # now contains numeric constants for flags
print(f"  - {len(rules['callbacks'])} callbacks")
print(f"  - {len(rules['includes'])} includes")

