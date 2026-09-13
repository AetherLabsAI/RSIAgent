"""Statically read an OSWorld public surface without importing evaluator code."""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PublicTaskSurface:
    class_path: Path
    instruction: str
    proxy: bool
    disable_vnc: bool
    disable_recording: bool
    has_setup: bool


def read_public_env_value(osworld_root: str | Path, name: str) -> str:
    osworld_root = Path(osworld_root)
    value = os.environ.get(name)
    if value is not None:
        return value
    env_path = osworld_root / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"public task instruction requires unset {name}") from exc
    prefix = name + "="
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):].strip().strip("'\"")
    raise RuntimeError(f"public task instruction requires unset {name}")


def load_public_task_surface(
        osworld_root: str | Path, task_id: str) -> PublicTaskSurface:
    """Statically extract reviewed public task metadata.

    Task modules are parsed as data and are never imported.  The deliberately
    small renderer accepts only static string expressions needed by the public
    instruction.  It rejects calls, attributes, subscripts, and arbitrary Python
    execution, so evaluator implementation and constants cannot enter an Agent
    context through this boundary.
    """
    root = Path(osworld_root).resolve()
    suffix = task_id.removeprefix("task_")
    if not suffix.isdigit() or not suffix:
        raise ValueError(f"invalid OSWorld task id: {task_id}")
    path = root / "evaluation_examples" / "task_class" / f"task_{suffix}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    module_values: dict[str, ast.expr] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            module_values[node.targets[0].id] = node.value

    expected_class = f"Task{suffix}"
    task_class = next(
        (node for node in tree.body
         if isinstance(node, ast.ClassDef) and node.name == expected_class),
        None,
    )
    if task_class is None:
        raise RuntimeError(f"public task class {expected_class} not found in {path}")
    class_values: dict[str, ast.expr] = {}
    for item in task_class.body:
        if (isinstance(item, ast.Assign) and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)):
            class_values[item.targets[0].id] = item.value
    if "instruction" not in class_values:
        raise RuntimeError(f"public instruction not found in {path}")

    resolved: dict[str, str] = {}
    resolving: set[str] = set()

    def render(node: ast.expr) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in resolved:
                return resolved[node.id]
            if node.id == "HOST_SUFFIX":
                resolved[node.id] = read_public_env_value(
                    root, "WEBSITE_HOST_SUFFIX")
                return resolved[node.id]
            if node.id in resolving or node.id not in module_values:
                raise RuntimeError(
                    f"unsupported public instruction name: {node.id}")
            resolving.add(node.id)
            try:
                resolved[node.id] = render(module_values[node.id])
            finally:
                resolving.remove(node.id)
            return resolved[node.id]
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    if value.conversion != -1 or value.format_spec is not None:
                        raise RuntimeError(
                            "formatted public instruction values are unsupported")
                    parts.append(render(value.value))
                else:
                    raise TypeError(
                        "unsupported public instruction f-string component")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return render(node.left) + render(node.right)
        raise RuntimeError(
            "public instruction must be a static string expression")

    def public_bool(name: str) -> bool:
        node = class_values.get(name)
        if node is None:
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value
        raise RuntimeError(f"public task field {name} must be a static boolean")

    instruction = render(class_values["instruction"])
    if not instruction.strip():
        raise RuntimeError(f"public instruction is empty in {path}")
    return PublicTaskSurface(
        class_path=path.resolve(),
        instruction=instruction,
        proxy=public_bool("proxy"),
        disable_vnc=public_bool("disable_vnc"),
        disable_recording=public_bool("disable_recording"),
        has_setup=any(
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "setup"
            for item in task_class.body),
    )


__all__ = [
    "PublicTaskSurface",
    "load_public_task_surface",
    "read_public_env_value",
]
