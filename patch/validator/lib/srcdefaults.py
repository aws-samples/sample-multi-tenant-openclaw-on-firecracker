import ast
import re
from pathlib import Path
def _scalar(text):
    raw = text.strip()
    if not raw:
        return ""
    raw = raw.split(" #", 1)[0].strip()
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none", "~"):
        return None
    try:
        return ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return raw.strip("\"'")
def config_defaults(path):
    out = {}
    stack = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(\s*)([A-Za-z0-9_]+):(?:\s*(.*))?$", line)
        if not match:
            continue
        indent, key, value = len(match.group(1)), match.group(2), match.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path_parts = [item[1] for item in stack] + [key]
        if value is not None and value.strip():
            out[".".join(path_parts)] = _scalar(value)
        else:
            stack.append((indent, key))
    return out
def _attr_name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))
def _eval(node, names, config):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_eval(item, names, config) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {_eval(k, names, config): _eval(v, names, config)
                for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _eval(node.operand, names, config)
        return -value if isinstance(value, (int, float)) else None
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, names, config), _eval(node.right, names, config)
        if isinstance(node.op, ast.Add) and left is not None and right is not None:
            return left + right
        if isinstance(node.op, ast.Mult) and left is not None and right is not None:
            return left * right
    if isinstance(node, ast.Subscript):
        base, key = _eval(node.value, names, config), _eval(node.slice, names, config)
        return base.get(key) if isinstance(base, dict) else None
    return _eval_call(node, names, config) if isinstance(node, ast.Call) else None
def _config_path(node):
    parts = []
    while isinstance(node, ast.Subscript):
        key = _eval(node.slice, {}, {})
        if not isinstance(key, str):
            return None
        parts.append(key)
        node = node.value
    if isinstance(node, ast.Name) and node.id == "CFG":
        return ".".join(reversed(parts))
    return None
def _eval_call(node, names, config):
    name = _attr_name(node.func)
    if name in ("str", "int", "float", "bool") and node.args:
        value = _eval(node.args[0], names, config)
        try:
            return {"str": str, "int": int, "float": float, "bool": bool}[name](value)
        except (TypeError, ValueError):
            return None
    if name.endswith(".get") and node.args:
        path = _config_path(node.func.value)
        key = _eval(node.args[0], names, config)
        default = _eval(node.args[1], names, config) if len(node.args) > 1 else None
        if path and isinstance(key, str):
            return config.get(path + "." + key, default)
    return None
def _trees(paths):
    for path in paths:
        try:
            yield path, ast.parse(path.read_text())
        except (OSError, SyntaxError):
            continue
def env_defaults(paths, config):
    out = {}
    for path, tree in _trees(paths):
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key_node, value_node in zip(node.keys, node.values):
                    key = _eval(key_node, {}, config)
                    value = _eval(value_node, {}, config)
                    if isinstance(key, str) and key.isupper() and value is not None:
                        out.setdefault(key, {"value": value, "source": "%s:%s" %
                                       (path.name, node.lineno), "parser": "string"})
            if isinstance(node, ast.Call):
                name = _attr_name(node.func)
                if name not in ("os.environ.get", "os.getenv") or not node.args:
                    continue
                key = _eval(node.args[0], {}, config)
                default = _eval(node.args[1], {}, config) if len(node.args) > 1 else None
                if isinstance(key, str):
                    out[key] = {"value": default, "source": "%s:%s" %
                                (path.name, node.lineno), "parser": "string"}
    _mark_parsers(paths, out)
    return out
def _mark_parsers(paths, defaults):
    for path, tree in _trees(paths):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            parser = _attr_name(node.func)
            inner = node.args[0]
            if parser not in ("int", "float") or not isinstance(inner, ast.Call):
                continue
            name = _attr_name(inner.func)
            key = _eval(inner.args[0], {}, {}) if inner.args else None
            if name in ("os.environ.get", "os.getenv") and key in defaults:
                defaults[key]["parser"] = parser
def literal_assignments(path, config):
    names = {"CFG": _nested(config)}
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return names
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if isinstance(target, ast.Name):
            value = _eval(node.value, names, config)
            if value is not None:
                names[target.id] = value
    return names
def _nested(flat):
    root = {}
    for dotted, value in flat.items():
        target = root
        parts = dotted.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return root


def route_defaults(path):
    routes, resources = [], {}
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return routes
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if _attr_name(call.func).endswith(".add_resource") and call.args:
                parent = _attr_name(call.func.value)
                base = "" if parent == "api.root" else resources.get(parent)
                segment = _eval(call.args[0], {}, {})
                if base is not None and isinstance(segment, str):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            resources[target.id] = base + "/" + segment
        if isinstance(node, ast.Call) and _attr_name(node.func).endswith(".add_method"):
            owner = _attr_name(node.func.value)
            method = _eval(node.args[0], {}, {}) if node.args else None
            if owner in resources and isinstance(method, str):
                routes.append({"path": resources[owner], "method": method.upper()})
    return routes


def parameter_specs(env):
    specs = {}
    for key, item in env.items():
        specs[key] = {
            "type": item.get("parser", "string"),
            "default": item.get("value"),
            "source": item.get("source"),
        }
    return specs


def collect(repo):
    root = Path(repo)
    config = config_defaults(root / "config.yml.example")
    py_files = list((root / "deploy/lambda/api").rglob("*.py"))
    py_files += list((root / "deploy/stacks").glob("*.py"))
    env = env_defaults(py_files, config)
    names = literal_assignments(
        root / "deploy/lambda/api/core/create_deadline.py", config)
    deadlines = names.get("_DEFAULT_DEADLINE_SEC") or {}
    return {
        "config": config,
        "env": env,
        "deadlines": deadlines if isinstance(deadlines, dict) else {},
        "routes": route_defaults(root / "deploy/stacks/lambdas.py"),
        "parameter_specs": parameter_specs(env),
    }
