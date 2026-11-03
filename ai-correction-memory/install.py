"""Install/upgrade managed skill files, with backup, edit detection and rollback."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import uuid
import zipfile

NAME, VERSION = "ai-correction-memory", "0.2.2"
MANIFEST = "package-manifest.json"
IGNORED = {"__pycache__", ".venv", ".git", "backups", "package-backups"}


def files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and not any(x in IGNORED for x in p.relative_to(root).parts)
            and p.suffix not in (".pyc", ".sqlite3", ".db") and p.name != MANIFEST}


def safe_relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(x in ("..", ".") or ":" in x or "\\" in x for x in path.parts):
        raise ValueError("Unsafe package path: " + name)
    return Path(*path.parts)


def install(source, destination, upgrade=False, check_only=False, backup_dir=None):
    source, destination = Path(source).resolve(), Path(destination).expanduser().resolve()
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("Choose a destination separate from the source package.")
    expected = files(source)
    if (source / MANIFEST).exists():
        package = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
        if package.get("name") != NAME or package.get("files") != expected:
            raise ValueError("Source package does not match its integrity manifest.")
    for name in expected:
        safe_relative(name)
    if destination.exists() and all((destination / safe_relative(n)).is_file() and
            hashlib.sha256((destination / safe_relative(n)).read_bytes()).hexdigest() == h for n, h in expected.items()):
        return {"status": "already_installed", "path": str(destination), "version": VERSION}
    old_hashes, old_version = {}, None
    if destination.exists():
        if not upgrade:
            raise ValueError("Destination already exists and differs. Use --upgrade after reviewing the change; nothing was modified.")
        if (destination / MANIFEST).exists():
            previous = json.loads((destination / MANIFEST).read_text(encoding="utf-8"))
            if previous.get("name") != NAME:
                raise ValueError("Destination is not a recognized installation.")
        else:
            previous = json.loads((source / "references/legacy-v0.1.0-hashes.json").read_text(encoding="utf-8"))
        old_hashes, old_version = previous["files"], previous["version"]
        for name, digest in old_hashes.items():
            path = destination / safe_relative(name)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Installed managed file was edited; refusing to overwrite: " + name)
        for name in expected:
            path = destination / safe_relative(name)
            if name not in old_hashes and path.exists():
                raise ValueError("New package would overwrite an unmanaged file: " + name)
    result = {"status": "ready", "path": str(destination), "version": VERSION,
              "previous_version": old_version, "files": len(expected), "database_modified": False}
    if check_only:
        return result
    backup = None
    if old_hashes:
        if backup_dir is None:
            # Import path helper only; this does not open or migrate a database.
            sys.path.insert(0, str(source / "scripts"))
            from memory import default_data_dir
            backup_dir = default_data_dir() / "package-backups"
        backup_dir = Path(backup_dir).expanduser().resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / ("before-" + old_version + "-" + uuid.uuid4().hex[:12] + ".zip")
        with zipfile.ZipFile(backup, "x", zipfile.ZIP_DEFLATED) as z:
            for name in old_hashes:
                z.write(destination / safe_relative(name), name)
            if (destination / MANIFEST).exists():
                z.write(destination / MANIFEST, MANIFEST)
    # Stage within the intended parent; cleanup is restricted to this exact path.
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = (destination.parent / (".ai-correction-stage-" + uuid.uuid4().hex)).resolve()
    assert stage.is_relative_to(destination.parent.resolve())
    stage.mkdir()
    before, changed = {}, []
    existed = destination.exists()
    try:
        for name in expected:
            rel = safe_relative(name)
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, stage / rel)
        manifest = {"name": NAME, "version": VERSION, "files": expected}
        (stage / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        destination.mkdir(exist_ok=True)
        for name in list(expected) + [MANIFEST]:
            rel = safe_relative(name)
            target = destination / rel
            before[name] = target.read_bytes() if target.exists() else None
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / rel, target)
            changed.append(name)
    except BaseException:
        for name in reversed(changed):
            target = destination / safe_relative(name)
            if before[name] is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(before[name])
        # Empty newly created directories are harmless; preserve any unrelated data.
        raise
    finally:
        assert stage.is_relative_to(destination.parent.resolve()) and stage.name.startswith(".ai-correction-stage-")
        shutil.rmtree(stage)
    result.update(status="upgraded" if existed else "installed", package_backup=str(backup) if backup else None)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--destination")
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--upgrade", action="store_true")
    p.add_argument("--backup-dir")
    args = p.parse_args()
    base = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    destination = Path(args.destination) if args.destination else base / "skills" / NAME
    print(json.dumps(install(Path(__file__).parent, destination, args.upgrade, args.check_only, args.backup_dir), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(2)
