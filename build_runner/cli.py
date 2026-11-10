"""Build Runner command line."""

import argparse
import json
from pathlib import Path
import re
import sys

# The Hub invokes the manifest entry as a file.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_runner import history, jobs, logtools, paramsets, workspaces
from build_runner.paths import ROOT, TOOL_ID, ToolError, data_dir, read_json, save_config


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError("INVALID_ARGUMENTS", message)


def parser():
    common = Parser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", help="Print a TOOL-SPEC JSON response")
    common.add_argument("--data-dir", help="Override the user data directory")
    common.add_argument("--dry-run", action="store_true", help="Preview without writing")
    common.add_argument("--yes", action="store_true", help="Confirm an overwrite or same-folder build")
    main = Parser(description=__doc__, parents=[common])
    main.add_argument("--version", action="store_true", help="Print the tool version")
    commands = main.add_subparsers(dest="command")
    commands.add_parser("workspaces", parents=[common], help="List registered workspaces and Git status")
    add = commands.add_parser("workspace-add", parents=[common], help="Register a workspace folder")
    add.add_argument("--name", required=True)
    add.add_argument("--path", required=True)
    trees = commands.add_parser("worktrees", parents=[common], help="List worktrees in a local Git repository")
    trees.add_argument("path")
    run = commands.add_parser("run", parents=[common], help="Create a build job")
    run.add_argument("--workspace", required=True)
    run.add_argument("--recipe", required=True)
    run.add_argument("--preset")
    run.add_argument("--wait", action="store_true", help="Wait for the job's final result")
    imports = commands.add_parser("import-params", parents=[common], help="Import a local parameter table")
    imports.add_argument("file", type=Path)
    imports.add_argument("--name", required=True)
    imports.add_argument("--command", dest="build_command", help="Override the table's build executable")
    commands.add_parser("jobs", parents=[common], help="List jobs")
    for name in ("status", "stop"):
        sub = commands.add_parser(name, parents=[common], help=f"{name.title()} a job")
        sub.add_argument("job_id")
    triage = commands.add_parser("triage", parents=[common], help="Analyze a job or log file")
    triage.add_argument("target")
    records = commands.add_parser("history", parents=[common], help="List or export build history")
    records.add_argument("--export", choices=("csv", "json"))
    records.add_argument("--out", type=Path)
    concurrency = commands.add_parser("concurrency", parents=[common], help="Read or update the concurrency limit")
    concurrency.add_argument("maximum", type=int, nargs="?")
    return main


def execute(args):
    data = data_dir(getattr(args, "data_dir", None))
    command = args.command
    warnings = []
    code = 0
    if command == "workspaces":
        latest = {row["workspaceId"]: row["state"] for row in history.list_history(data)}
        result = [{**row, **workspaces.git_state(row["path"]), "lastResult": latest.get(row["id"])}
                  for row in workspaces.list_workspaces(data)]
    elif command == "workspace-add":
        if args.dry_run:
            result = {"name": args.name, "path": str(Path(args.path).expanduser().resolve())}
        else:
            result = workspaces.add_workspace(data, args.name, args.path)
    elif command == "worktrees":
        result = workspaces.list_worktrees(args.path)
    elif command == "import-params":
        with args.file.open("rb") as stream:
            raw = stream.read(paramsets.MAX_SOURCE_BYTES + 1)
        result = paramsets.parse(raw, args.file.name, args.name, args.build_command)
        if not args.dry_run:
            save_config(data, "paramsets", result, overwrite=args.yes)
    elif command == "run":
        workspace = workspaces.get_workspace(data, args.workspace)
        result, warnings = jobs.start(data, workspace, args.recipe, args.preset, force=args.yes, dry_run=args.dry_run)
        if args.wait and not args.dry_run:
            result = jobs.wait(data, result["id"])
        code = {"failed": 1, "stopped": 4}.get(result.get("state"), 0)
    elif command == "jobs":
        result = jobs.list_jobs(data)
    elif command == "status":
        result = jobs.get_job(data, args.job_id)
    elif command == "stop":
        result = jobs.get_job(data, args.job_id) if args.dry_run else jobs.stop(data, args.job_id)
    elif command == "triage":
        path = Path(args.target).expanduser()
        if not path.is_file():
            if not re.fullmatch(r"[a-f0-9]{32}", args.target):
                raise ToolError("LOG_NOT_FOUND", f"Log not found: {path}")
            jobs.get_job(data, args.target)
            path = jobs.job_dir(data, args.target) / "build.log"
        result = logtools.triage(path)
    elif command == "history":
        if args.out and not args.export:
            raise ToolError("INVALID_ARGUMENTS", "--out requires --export.")
        result = history.list_history(data)
        if args.export:
            content = history.export(result, args.export)
            if args.out:
                if not args.dry_run:
                    history.write_export(args.out, content, overwrite=args.yes)
                result = {"path": str(args.out), "records": len(result), "format": args.export}
            else:
                result = content
    elif command == "concurrency":
        value = args.maximum
        if value is None:
            value = jobs.limit(data)
        elif args.dry_run:
            if value < 0:
                raise ToolError("INVALID_CONCURRENCY", "Concurrency must be a nonnegative integer.")
        else:
            value = jobs.set_limit(data, value)
        result = {"maxConcurrentJobs": value}
    return result, warnings, code


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    cli = parser()
    json_mode = "--json" in argv
    dry_run = "--dry-run" in argv
    command = None
    result, warnings, error = None, [], None
    try:
        args = cli.parse_args(argv)
        args.yes = getattr(args, "yes", False)
        args.dry_run = getattr(args, "dry_run", False)
        command = args.command
        if args.version:
            result, code = read_json(ROOT / "tool.json")["version"], 0
        elif not command:
            cli.print_help()
            return 0
        else:
            result, warnings, code = execute(args)
            if code:
                error = {"code": "BUILD_STOPPED" if code == 4 else "BUILD_FAILED",
                         "message": f"Build {result['state']}.", "detail": {"jobId": result["id"]}}
    except ToolError as exc:
        code = exc.exit_code
        error = {"code": exc.code, "message": str(exc), "detail": exc.detail}
    except OSError as exc:
        code = 1
        error = {"code": "IO_ERROR", "message": str(exc), "detail": {}}
    except KeyboardInterrupt:
        code = 4
        error = {"code": "CANCELLED", "message": "Command cancelled.", "detail": {}}

    if json_mode:
        print(json.dumps({"ok": code == 0, "tool": TOOL_ID, "command": command, "dryRun": dry_run,
                          "result": result, "warnings": warnings, "error": error}, ensure_ascii=False))
    else:
        if result is not None:
            if isinstance(result, str):
                print(result, end="" if result.endswith("\n") else "\n")
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
        for warning in warnings:
            print(warning["message"], file=sys.stderr)
        if error:
            print(f'{error["code"]}: {error["message"]}', file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
