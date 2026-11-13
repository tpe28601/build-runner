"""Load recipes and resolve the exact steps a runner will execute."""

import os
from pathlib import Path
import re

from . import paramsets
from .paths import ROOT, ToolError, config_records, named_file, read_json

BUILTIN_ID = "@builtin:clean-build"
BUILTIN_NOU_ID = "@builtin:clean-build-nou"
BUILTIN_PACKAGE_ID = "@builtin:clean-build-package"
BUILTIN_RECIPES = (
    (BUILTIN_ID, "recipe-sample.json"),
    (BUILTIN_NOU_ID, "recipe-nou-sample.json"),
    (BUILTIN_PACKAGE_ID, "recipe-package-sample.json"),
)
BUILTIN_IDS = {builtin_id for builtin_id, _ in BUILTIN_RECIPES}


def builtin_recipe(record_id=BUILTIN_ID):
    sample = dict(BUILTIN_RECIPES)[record_id]
    return {**read_json(ROOT / "examples" / sample), "id": record_id, "builtIn": True}


def builtin_recipes():
    return [builtin_recipe(builtin_id) for builtin_id, _ in BUILTIN_RECIPES]


def list_recipes(data):
    return [*builtin_recipes(), *({**row, "builtIn": False} for row in config_records(data, "recipes"))]


def get_recipe(data, name):
    if name in BUILTIN_IDS:
        return builtin_recipe(name)
    for builtin in builtin_recipes():
        if builtin["name"].casefold() == name.casefold():
            return builtin
    path = named_file(data, "recipes", name)
    recipe = read_json(path)
    if recipe is None:
        for candidate in sorted(path.parent.glob("*.json")):
            record = read_json(candidate)
            if isinstance(record, dict) and str(record.get("name", "")).casefold() == name.casefold():
                recipe = record
                break
    if recipe is None:
        raise ToolError("RECIPE_NOT_FOUND", f"Recipe not found: {name}")
    if not isinstance(recipe, dict) or not isinstance(recipe.get("steps"), list):
        raise ToolError("INVALID_RECIPE", "Recipe must contain a list of steps.")
    return recipe


def prepare(workspace, recipe, preset=None, *, table=None, overrides=None):
    root = Path(workspace["path"]).resolve()
    if not root.is_dir():
        raise ToolError("INVALID_WORKSPACE", f"Workspace folder does not exist: {root}")
    env = recipe.get("env", {})
    if not isinstance(env, dict) or any(
        not key or "=" in key or "\0" in key or not isinstance(value, str) or "\0" in value
        for key, value in env.items()
    ):
        raise ToolError("INVALID_RECIPE", "Recipe environment must contain valid names and string values.")
    timeout = recipe.get("timeoutMin", 180)
    if type(timeout) is not int or timeout < 1:
        raise ToolError("INVALID_RECIPE", "timeoutMin must be a positive integer.")
    steps = []
    values = {}
    used_table = False
    for index, step in enumerate(recipe["steps"], 1):
        if not isinstance(step, dict):
            raise ToolError("INVALID_RECIPE", f"Invalid step {index}.")
        if step.get("enabled") is False:
            continue
        argv = step.get("argv")
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]):
            raise ToolError("INVALID_RECIPE", f"Step {index} must provide a nonempty argv array.")
        if step.get("useParams") and table is not None:
            args, values = paramsets.resolve(table, preset if preset is not None else step.get("preset"), overrides)
            argv = [*argv, *args]
            used_table = True
        elif step.get("useParams") and step.get("preset"):
            raise ToolError("PARAMSET_REQUIRED", "A preset requires a parameter table.")
        cwd = step.get("cwd", ".")
        if not isinstance(cwd, str):
            raise ToolError("INVALID_RECIPE", f"Invalid cwd for step {index}.")
        cwd = (root / cwd).resolve()
        if not cwd.is_relative_to(root) or not cwd.is_dir():
            raise ToolError("INVALID_RECIPE", f"Step {index} cwd must be an existing folder inside the workspace.")
        if recipe.get("id") in BUILTIN_IDS:
            script = cwd / argv[0]
            if not script.is_file():
                raise ToolError("BUILTIN_SCRIPT_NOT_FOUND", "Clean + Build requires builda.bat in the Workspace root.")
            argv = [str(script), *argv[1:]]
        steps.append({
            "name": step.get("name") or f"Step {index}",
            "argv": argv,
            "batch": validate_batch(argv),
            "cwd": str(cwd),
            "continueOnFail": step.get("continueOnFail") is True,
        })
    if not steps:
        raise ToolError("INVALID_RECIPE", "Recipe has no enabled steps.")
    if not used_table and (preset is not None or overrides):
        raise ToolError("PARAMSET_REQUIRED", "This Recipe has no enabled step that uses a parameter table.")
    return {"workspacePath": str(root), "steps": steps, "env": env, "timeoutSec": timeout * 60, "params": values}


def validate_batch(argv):
    if argv[0].endswith((" ", ".")):
        raise ToolError("INVALID_EXECUTABLE", "Executable names cannot end with a space or dot.")
    if Path(argv[0]).suffix.lower() not in (".bat", ".cmd"):
        return False
    if os.name != "nt":
        raise ToolError("WINDOWS_REQUIRED", "Batch files require Windows.", 5)
    if any(re.search(r'["\r\n]', arg) for arg in argv):
        raise ToolError("UNSAFE_BATCH_ARGUMENT", "Batch arguments cannot contain double quotes or newlines.")
    return True


def launch_args(step, env):
    argv = step["argv"]
    if step["batch"]:
        # Expand each argument once inside cmd. An initial IF protects the first
        # argument's quotes, and delayed expansion must stay disabled for '!'.
        env = env.copy()
        command = ["cmd.exe", "/d", "/v:off", "/c", "if", "1==1"]
        for index, arg in enumerate(argv):
            key = f"DEVTOOLBOX_BUILD_ARG_{index}"
            env[key] = f'"{arg}"' if not arg or re.search(r'[\s&|<>()^,;=]', arg) else arg
            command.append(f"%{key}%")
        executable = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        return command, str(executable), env

    # On Windows CreateProcess resolves the executable before changing cwd.
    cwd = Path(step["cwd"])
    name = Path(argv[0])
    if not name.suffix:
        name = name.with_name(name.name + ".exe")
    candidate = cwd / name
    if candidate.is_file() or Path(argv[0]).name != argv[0]:
        return argv, str(candidate), env
    # Search only for the requested native filename. PATHEXT could resolve an
    # unchecked batch file, and CreateProcess ignores PATH in the child env.
    for directory in env.get("PATH", "").split(os.pathsep):
        if directory:
            candidate = cwd / directory.strip('"') / name
            if candidate.is_file():
                return argv, str(candidate), env
    return argv, None, env
