from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import Any, Callable

from ..graph import Node

from ..registry import FlowOutputs, FlowRegistry, NodeNamespace
from ..seed import SafeMath, SeededRandom


class FlowSecurityError(SyntaxError):
    pass


class _Return(Exception):
    def __init__(self, value):
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class Env(dict):
    def __init__(self, *args, parent: "Env | None" = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.parent = parent

    def resolve(self, name: str):
        if name in self:
            return self[name]
        if self.parent is not None:
            return self.parent.resolve(name)
        raise NameError(name)


@dataclass
class UserFunction:
    node: ast.FunctionDef
    evaluator: "SafeEvaluator"
    closure: Env

    def __call__(self, *args, **kwargs):
        fn = self.node
        params = list(fn.args.posonlyargs) + list(fn.args.args)
        if len(args) > len(params):
            raise TypeError(f"{fn.name}() takes at most {len(params)} positional arguments")

        local = Env(parent=self.closure)
        assigned = set()
        for arg, value in zip(params, args):
            local[arg.arg] = value
            assigned.add(arg.arg)
        for name, value in kwargs.items():
            if name not in {a.arg for a in params}:
                raise TypeError(f"{fn.name}() got an unexpected keyword argument {name!r}")
            if name in assigned:
                raise TypeError(f"{fn.name}() got multiple values for {name!r}")
            local[name] = value
            assigned.add(name)

        defaults = [None] * (len(params) - len(fn.args.defaults)) + list(fn.args.defaults)
        for arg, default in zip(params, defaults):
            if arg.arg in local:
                continue
            if default is None:
                raise TypeError(f"{fn.name}() missing required argument {arg.arg!r}")
            local[arg.arg] = self.evaluator.eval_expr(default, self.closure)

        try:
            self.evaluator.exec_block(fn.body, local)
        except _Return as ret:
            return ret.value
        return None


class SafeEvaluator:
    _binops: dict[type[ast.operator], Callable] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.BitOr: operator.or_,
        ast.BitAnd: operator.and_,
        ast.BitXor: operator.xor,
        ast.LShift: operator.lshift,
        ast.RShift: operator.rshift,
    }
    _unary = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_, ast.Invert: operator.invert}
    _compare = {
        ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b, ast.Is: operator.is_, ast.IsNot: operator.is_not,
    }
    _safe_methods = {
        str: {"capitalize", "casefold", "count", "endswith", "find", "format", "index", "isalnum", "isalpha", "isdigit", "islower", "isspace", "istitle", "isupper", "join", "lower", "lstrip", "partition", "removeprefix", "removesuffix", "replace", "rfind", "rindex", "rpartition", "rsplit", "rstrip", "split", "splitlines", "startswith", "strip", "swapcase", "title", "upper", "zfill"},
        list: {"append", "clear", "copy", "count", "extend", "index", "insert", "pop", "remove", "reverse", "sort"},
        dict: {"clear", "copy", "get", "items", "keys", "pop", "popitem", "setdefault", "update", "values"},
        set: {"add", "clear", "copy", "difference", "discard", "intersection", "isdisjoint", "issubset", "issuperset", "pop", "remove", "union", "update"},
        tuple: {"count", "index"},
    }
    _builtins = {
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
        "enumerate": enumerate, "filter": filter, "float": float, "int": int,
        "len": len, "list": list, "map": map, "max": max, "min": min,
        "range": range, "reversed": reversed, "round": round, "set": set,
        "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    }

    def __init__(self, registry: FlowRegistry, global_seed: int):
        self.registry = registry
        self.global_seed = int(global_seed)
        self.globals = Env({
            **self._builtins,
            "global_seed": self.global_seed,
            "seed": self.global_seed,
            "random": SeededRandom(self.global_seed),
            "math": SafeMath(),
            "nodes": registry.nodes,
        })

    def prepare(self, tree: ast.Module) -> Env:
        for stmt in tree.body:
            if isinstance(stmt, ast.FunctionDef):
                self.globals[stmt.name] = UserFunction(stmt, self, self.globals)
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                self.exec_stmt(stmt, self.globals)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                self.exec_stmt(stmt, self.globals)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue  # module docstring
            else:
                raise FlowSecurityError(f"Only imports, constants, and function definitions are allowed at module scope ({type(stmt).__name__})")
        return self.globals

    def exec_block(self, statements: list[ast.stmt], env: Env):
        for stmt in statements:
            self.exec_stmt(stmt, env)

    def exec_stmt(self, node: ast.stmt, env: Env):
        if isinstance(node, ast.Assign):
            value = self.eval_expr(node.value, env)
            for target in node.targets:
                self.assign(target, value, env)
            return
        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self.assign(node.target, self.eval_expr(node.value, env), env)
            return
        if isinstance(node, ast.AugAssign):
            old = self.eval_expr(node.target, env)
            op = self._binops.get(type(node.op))
            if op is None:
                raise FlowSecurityError(f"Unsupported operator: {type(node.op).__name__}")
            self.assign(node.target, op(old, self.eval_expr(node.value, env)), env)
            return
        if isinstance(node, ast.Expr):
            self.eval_expr(node.value, env)
            return
        if isinstance(node, ast.Return):
            raise _Return(None if node.value is None else self.eval_expr(node.value, env))
        if isinstance(node, ast.If):
            block = node.body if self.eval_expr(node.test, env) else node.orelse
            self.exec_block(block, env)
            return
        if isinstance(node, ast.For):
            iterable = self.eval_expr(node.iter, env)
            for value in iterable:
                self.assign(node.target, value, env)
                try:
                    self.exec_block(node.body, env)
                except _Continue:
                    continue
                except _Break:
                    break
            else:
                self.exec_block(node.orelse, env)
            return
        if isinstance(node, ast.Match):
            subject = self.eval_expr(node.subject, env)
            for case in node.cases:
                captures: dict[str, Any] = {}
                if not self.match_pattern(case.pattern, subject, env, captures):
                    continue
                # Python match/case is block-like syntactically but does not create
                # a new local scope. Captures and assignments remain visible.
                env.update(captures)
                if case.guard is not None and not self.eval_expr(case.guard, env):
                    continue
                self.exec_block(case.body, env)
                return
            return
        if isinstance(node, ast.Break):
            raise _Break()
        if isinstance(node, ast.Continue):
            raise _Continue()
        if isinstance(node, ast.FunctionDef):
            env[node.name] = UserFunction(node, self, env)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("math", "random"):
                    value = env.resolve(alias.name)
                else:
                    value = self.registry.namespace(alias.name)
                env[alias.asname or alias.name] = value
            return
        if isinstance(node, ast.ImportFrom):
            if node.level:
                raise FlowSecurityError("Relative imports are not supported")
            namespace = self.registry.namespace(node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    raise FlowSecurityError("Wildcard imports are not supported")
                env[alias.asname or alias.name] = getattr(namespace, alias.name)
            return
        if isinstance(node, ast.Pass):
            return
        raise FlowSecurityError(f"Unsupported statement: {type(node).__name__}")

    def match_pattern(self, pattern: ast.pattern, subject: Any, env: Env, captures: dict[str, Any]) -> bool:
        if isinstance(pattern, ast.MatchValue):
            return subject == self.eval_expr(pattern.value, env)
        if isinstance(pattern, ast.MatchSingleton):
            return subject is pattern.value
        if isinstance(pattern, ast.MatchAs):
            if pattern.pattern is not None and not self.match_pattern(pattern.pattern, subject, env, captures):
                return False
            if pattern.name is not None:
                captures[pattern.name] = subject
            return True
        if isinstance(pattern, ast.MatchOr):
            for child in pattern.patterns:
                branch: dict[str, Any] = {}
                if self.match_pattern(child, subject, env, branch):
                    captures.update(branch)
                    return True
            return False
        if isinstance(pattern, ast.MatchSequence):
            if isinstance(subject, (str, bytes)):
                return False
            try:
                values = list(subject)
            except TypeError:
                return False
            star_indexes = [i for i, child in enumerate(pattern.patterns) if isinstance(child, ast.MatchStar)]
            if len(star_indexes) > 1:
                raise FlowSecurityError("Only one starred match item is allowed")
            if not star_indexes:
                if len(values) != len(pattern.patterns):
                    return False
                pairs = zip(pattern.patterns, values)
                for child, value in pairs:
                    if not self.match_pattern(child, value, env, captures):
                        return False
                return True
            star = star_indexes[0]
            before = pattern.patterns[:star]
            after = pattern.patterns[star + 1:]
            if len(values) < len(before) + len(after):
                return False
            for child, value in zip(before, values[:len(before)]):
                if not self.match_pattern(child, value, env, captures):
                    return False
            if after:
                for child, value in zip(after, values[-len(after):]):
                    if not self.match_pattern(child, value, env, captures):
                        return False
            star_pattern = pattern.patterns[star]
            if star_pattern.name is not None:
                end = len(values) - len(after) if after else len(values)
                captures[star_pattern.name] = values[len(before):end]
            return True
        if isinstance(pattern, ast.MatchMapping):
            if not isinstance(subject, dict):
                return False
            used = set()
            for key_node, child in zip(pattern.keys, pattern.patterns):
                key = self.eval_expr(key_node, env)
                if key not in subject or not self.match_pattern(child, subject[key], env, captures):
                    return False
                used.add(key)
            if pattern.rest is not None:
                captures[pattern.rest] = {k: v for k, v in subject.items() if k not in used}
            return True
        if isinstance(pattern, ast.MatchStar):
            if pattern.name is not None:
                captures[pattern.name] = subject
            return True
        raise FlowSecurityError(f"Unsupported match pattern: {type(pattern).__name__}")

    def assign(self, target: ast.expr, value: Any, env: Env):
        if isinstance(target, ast.Name):
            if target.id in {"global_seed", "random", "math", "nodes"}:
                raise FlowSecurityError(f"Cannot assign reserved name {target.id!r}")
            env[target.id] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            values = list(value)
            if len(values) != len(target.elts):
                raise ValueError("unpack length mismatch")
            for sub, item in zip(target.elts, values):
                self.assign(sub, item, env)
            return
        if isinstance(target, ast.Subscript):
            obj = self.eval_expr(target.value, env)
            if not isinstance(obj, (list, dict)):
                raise FlowSecurityError("Only list/dict item assignment is allowed")
            obj[self.eval_slice(target.slice, env)] = value
            return
        raise FlowSecurityError(f"Unsupported assignment target: {type(target).__name__}")

    def eval_slice(self, node: ast.expr, env: Env):
        if isinstance(node, ast.Slice):
            return slice(
                None if node.lower is None else self.eval_expr(node.lower, env),
                None if node.upper is None else self.eval_expr(node.upper, env),
                None if node.step is None else self.eval_expr(node.step, env),
            )
        return self.eval_expr(node, env)

    def eval_expr(self, node: ast.expr, env: Env):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env.resolve(node.id)
        if isinstance(node, ast.List):
            return [self.eval_expr(v, env) for v in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self.eval_expr(v, env) for v in node.elts)
        if isinstance(node, ast.Set):
            return {self.eval_expr(v, env) for v in node.elts}
        if isinstance(node, ast.Dict):
            return {self.eval_expr(k, env): self.eval_expr(v, env) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.BinOp):
            op = self._binops.get(type(node.op))
            if op is None:
                raise FlowSecurityError(f"Unsupported operator: {type(node.op).__name__}")
            return op(self.eval_expr(node.left, env), self.eval_expr(node.right, env))
        if isinstance(node, ast.UnaryOp):
            op = self._unary.get(type(node.op))
            if op is None:
                raise FlowSecurityError(f"Unsupported unary operator: {type(node.op).__name__}")
            return op(self.eval_expr(node.operand, env))
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = True
                for value in node.values:
                    result = self.eval_expr(value, env)
                    if not result:
                        return result
                return result
            if isinstance(node.op, ast.Or):
                result = False
                for value in node.values:
                    result = self.eval_expr(value, env)
                    if result:
                        return result
                return result
        if isinstance(node, ast.Compare):
            left = self.eval_expr(node.left, env)
            for op_node, comp in zip(node.ops, node.comparators):
                right = self.eval_expr(comp, env)
                op = self._compare.get(type(op_node))
                if op is None or not op(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self.eval_expr(node.body if self.eval_expr(node.test, env) else node.orelse, env)
        if isinstance(node, ast.Subscript):
            return self.eval_expr(node.value, env)[self.eval_slice(node.slice, env)]
        if isinstance(node, ast.Attribute):
            return self.safe_attribute(self.eval_expr(node.value, env), node.attr)
        if isinstance(node, ast.Call):
            fn = self.eval_expr(node.func, env)
            if not self.is_safe_callable(fn):
                raise FlowSecurityError(f"Call target is not allowed: {ast.unparse(node.func)}")
            args = [self.eval_expr(a, env) for a in node.args]
            kwargs = {kw.arg: self.eval_expr(kw.value, env) for kw in node.keywords if kw.arg is not None}
            if any(kw.arg is None for kw in node.keywords):
                raise FlowSecurityError("**kwargs expansion is not supported")
            return fn(*args, **kwargs)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(str(value.value))
                else:
                    parts.append(str(self.eval_expr(value.value, env)))
            return "".join(parts)
        if isinstance(node, ast.FormattedValue):
            return self.eval_expr(node.value, env)
        if isinstance(node, ast.ListComp):
            return list(self.eval_comprehension(node.elt, node.generators, env))
        if isinstance(node, ast.SetComp):
            return set(self.eval_comprehension(node.elt, node.generators, env))
        if isinstance(node, ast.GeneratorExp):
            return iter(self.eval_comprehension(node.elt, node.generators, env))
        if isinstance(node, ast.DictComp):
            result = {}
            for local in self.iter_comprehension_envs(node.generators, env):
                result[self.eval_expr(node.key, local)] = self.eval_expr(node.value, local)
            return result
        raise FlowSecurityError(f"Unsupported expression: {type(node).__name__}")

    def iter_comprehension_envs(self, generators: list[ast.comprehension], env: Env, index: int = 0):
        if index >= len(generators):
            yield env
            return
        gen = generators[index]
        if gen.is_async:
            raise FlowSecurityError("Async comprehensions are not supported")
        for value in self.eval_expr(gen.iter, env):
            local = Env(parent=env)
            self.assign(gen.target, value, local)
            if all(self.eval_expr(cond, local) for cond in gen.ifs):
                yield from self.iter_comprehension_envs(generators, local, index + 1)

    def eval_comprehension(self, expression: ast.expr, generators: list[ast.comprehension], env: Env):
        for local in self.iter_comprehension_envs(generators, env):
            yield self.eval_expr(expression, local)

    def safe_attribute(self, obj: Any, name: str):
        if name.startswith("_"):
            raise FlowSecurityError("Private/dunder attributes are not accessible")
        if isinstance(obj, NodeNamespace):
            return getattr(obj, name)
        if isinstance(obj, FlowOutputs):
            return getattr(obj, name)
        # FlowSingleOutput and other deliberately small KSTR wrappers expose only
        # declared public attributes through __getattr__.
        if obj.__class__.__module__.startswith("kstr_flow") and not isinstance(obj, (SafeMath, SeededRandom)):
            return getattr(obj, name)
        if isinstance(obj, (SafeMath, SeededRandom)):
            return getattr(obj, name)
        for typ, methods in self._safe_methods.items():
            if isinstance(obj, typ) and name in methods:
                return getattr(obj, name)
        raise FlowSecurityError(f"Attribute access is not allowed: {type(obj).__name__}.{name}")

    def is_safe_callable(self, fn: Any) -> bool:
        if isinstance(fn, (UserFunction, Node)):
            return True
        if fn in self._builtins.values():
            return True
        owner = getattr(fn, "__self__", None)
        if isinstance(owner, (SafeMath, SeededRandom, str, list, dict, set, tuple)):
            return True
        # SafeMath exposes stdlib math callables directly; SeededRandom exposes
        # methods bound to its private random.Random instance. Those values can
        # only be obtained through the guarded namespaces above.
        if getattr(fn, "__module__", None) == "math":
            return True
        if owner is not None and owner.__class__.__module__ == "random" and owner.__class__.__name__ == "Random":
            return True
        return False
