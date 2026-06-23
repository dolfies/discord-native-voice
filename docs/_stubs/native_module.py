from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _unavailable() -> RuntimeError:
    return RuntimeError('discord.ext.native_voice._native_voice is not available during documentation builds')


def _make_function(name: str, qualname: str) -> Any:
    def function(*args: Any, **kwargs: Any) -> Any:
        raise _unavailable()

    function.__name__ = name
    function.__qualname__ = qualname
    return function


def _make_class(name: str, module_name: str, node: ast.ClassDef) -> type[Any]:
    namespace: dict[str, Any] = {
        '__module__': module_name,
        '__annotations__': {},
    }

    for item in node.body:
        if isinstance(item, ast.FunctionDef):
            namespace[item.name] = _make_function(item.name, f'{name}.{item.name}')
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            namespace['__annotations__'][item.target.id] = Any

    return type(name, (), namespace)


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    if isinstance(node, ast.AnnAssign):
        return (node.target.id,) if isinstance(node.target, ast.Name) else ()

    names: list[str] = []
    for target in node.targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
    return tuple(names)


def install_native_voice_stub(module_name: str, pyi_path: Path) -> None:
    tree = ast.parse(pyi_path.read_text(encoding='utf-8'), filename=str(pyi_path))
    module = ModuleType(module_name)
    module.__file__ = str(pyi_path)
    module.__all__ = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            setattr(module, node.name, _make_class(node.name, module_name, node))
            module.__all__.append(node.name)
        elif isinstance(node, ast.FunctionDef):
            setattr(module, node.name, _make_function(node.name, node.name))
            module.__all__.append(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in _assigned_names(node):
                setattr(module, name, Any)
                module.__all__.append(name)

    sys.modules[module_name] = module
