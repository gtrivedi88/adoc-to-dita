"""Read immutable Git snapshots without checking out or modifying user branches."""
from __future__ import annotations

import fnmatch
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit

from .converter import ROOT, convert_files
from .report import file_diff


def git(repo, *args, timeout=180):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    run = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env, timeout=timeout)
    if run.returncode:
        raise ValueError(run.stderr.decode(errors="replace").strip() or "Git command failed")
    return run.stdout


def remote_url(value):
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname != "github.com" or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Remote repositories must use https://github.com/OWNER/REPO; local paths also work")
    pieces = parts.path.strip("/").removesuffix(".git").split("/")
    if len(pieces) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", x) and x not in (".", "..") for x in pieces):
        raise ValueError("Use a repository URL, without /tree/ or /blob/")
    return "https://github.com/" + "/".join(pieces) + ".git"


def refs(repo):
    if str(repo).startswith("https:"):
        url = remote_url(str(repo))
        raw = git(ROOT, "ls-remote", "--heads", "--tags", url).decode()
        return sorted({line.split("\t")[1].removeprefix("refs/heads/").removeprefix("refs/tags/") for line in raw.splitlines() if not line.endswith("^{}")})
    return git(Path(repo).expanduser().resolve(), "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/tags", "refs/remotes").decode().splitlines()


def resolve(repo, ref):
    if not ref or ref.startswith("-") or "\x00" in ref:
        raise ValueError("Select a branch, tag, or commit")
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def prepare_repository(value, base, target, cache=None):
    if not str(value).startswith("https:"):
        repo = Path(value).expanduser().resolve()
        return repo, resolve(repo, base), resolve(repo, target)
    url = remote_url(str(value))
    cache = Path(cache or ROOT / ".cache/repos")
    repo = cache / hashlib.sha256(url.encode()).hexdigest()[:24]
    cache.mkdir(parents=True, exist_ok=True)
    # Git's own lock files survive a killed process. Serialize this private
    # cache across CLI processes and remove those stale transaction files only
    # after acquiring our independent lock.
    with (cache / (repo.name + ".adoc-dita.lock")).open("a+b") as cache_lock:
        fcntl.flock(cache_lock, fcntl.LOCK_EX)
        if repo.is_symlink():
            raise ValueError("Invalid remote repository cache")
        repo.mkdir(parents=True, exist_ok=True)
        for lock in repo.rglob("*.lock"):
            if lock.is_file() or lock.is_symlink():
                lock.unlink()
        if not (repo / "HEAD").exists():
            git(repo, "init", "--bare")
        advertised = {}
        for line in git(ROOT, "ls-remote", "--heads", "--tags", url).decode().splitlines():
            sha, name = line.split("\t")
            advertised[name] = sha

        def fetch(ref):
            if not ref or ref.startswith("-"):
                raise ValueError("Invalid release reference")
            names = [ref] if ref.startswith("refs/") else ["refs/heads/" + ref, "refs/tags/" + ref]
            names = [name for name in names if name in advertised]
            if len(names) > 1:
                raise ValueError(f"Ambiguous branch/tag {ref}; use refs/heads/ or refs/tags/")
            if names:
                sha = advertised.get(names[0] + "^{}", advertised[names[0]])
            elif re.fullmatch(r"[0-9a-fA-F]{40}", ref):
                sha = ref
            else:
                raise ValueError(f"Reference not found: {ref}")
            # Fetch only the chosen snapshot, pinned to the advertised object ID.
            git(repo, "fetch", "--no-tags", "--depth=1", url, sha)
            return resolve(repo, sha)

        base_sha, target_sha = fetch(base), fetch(target)
    return repo, base_sha, target_sha


def snapshot(repo, commit, destination):
    data = git(repo, "archive", "--format=tar", commit, timeout=240)
    if len(data) > 300 * 1024 * 1024:
        raise ValueError("Snapshot exceeds the 300 MB limit")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        links = []
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe path in Git archive")
            if member.issym():
                links.append((path, member.linkname))
            if not member.isfile():
                continue
            output = destination.joinpath(*path.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.extractfile(member).read())
        # Preserve repository include aliases, but never links outside the snapshot.
        for path, target in links:
            output = destination.joinpath(*path.parts)
            if Path(target).is_absolute() or not (output.parent / target).resolve().is_relative_to(destination.resolve()):
                continue
            if output.exists() or output.is_symlink():
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            output.symlink_to(target)
        for path, _ in links:
            output = destination.joinpath(*path.parts)
            if output.is_symlink() and not output.resolve().is_relative_to(destination.resolve()):
                output.unlink()


def changed_paths(repo, base, target):
    pieces = git(repo, "diff", "--name-status", "-z", "--find-renames=50%", base, target).decode().split("\0")
    result = []
    index = 0
    while index < len(pieces) and pieces[index]:
        status, path = pieces[index:index + 2]
        index += 2
        if status.startswith("R"):
            result.append({"change": "renamed", "before": path, "after": pieces[index]})
            index += 1
        else:
            result.append({"change": {"A": "added", "D": "deleted"}.get(status[0], "modified"),
                           "before": None if status == "A" else path, "after": None if status == "D" else path})
    return result


def topic_paths(root, patterns, kind="auto"):
    selected = []
    for file in sorted(root.rglob("*.adoc")):
        relative = file.relative_to(root).as_posix()
        if not any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
            continue
        text = file.read_text(encoding="utf-8")
        if re.search(r"^:_(?:mod-docs-content-type|content-type|module-type):\s*(SNIPPET|ATTRIBUTES)\s*$", text, re.M | re.I):
            continue
        if kind == "auto" and file.name.endswith(".template.adoc"):
            continue
        declared_type = re.search(r"^:_(?:mod-docs-content-type|content-type|module-type):\s*(CONCEPT|PROCEDURE|REFERENCE)\s*$", text, re.M | re.I)
        named_type = file.name.startswith(("con-", "proc-", "ref-"))
        # In automatic mode, only select sources whose topic specialization is
        # deterministic. Assemblies and guide entry documents still remain in
        # the Git change/dependency inventory, but are not emitted as topics.
        explicitly_typed = kind != "auto" and re.search(r"^=\s+\S", text, re.M)
        if declared_type or named_type or explicitly_typed:
            selected.append(relative)
    return selected


def compare(repository, base, target, *, patterns=None, attributes=None, attribute_files=None,
            attribute_text="", attribute_filename=None, kind="auto", progress=None, guide=None):
    progress = progress or (lambda message: None)
    patterns = patterns or ["*.adoc"]
    attribute_text = attribute_text or ""
    if attribute_text:
        if len(attribute_text.encode()) > 2 * 1024 * 1024:
            raise ValueError("Uploaded attributes file must be smaller than 2 MB")
        if not attribute_filename or Path(attribute_filename).name != attribute_filename or not attribute_filename.lower().endswith(".adoc"):
            raise ValueError("Uploaded attributes must use a filename ending in .adoc")
        if re.search(r"^include::", attribute_text, re.M):
            raise ValueError("Uploaded comparison attributes cannot include other files. Use repository-relative attribute files for include chains.")
    progress("Resolving the selected Git snapshots…")
    repo, base_sha, target_sha = prepare_repository(repository, base, target)
    changes = changed_paths(repo, base_sha, target_sha)
    touched = {p for change in changes for p in [change["before"], change["after"]] if p}
    report = {"format_version": 1, "repository": str(repository), "base": {"ref": base, "commit": base_sha},
              "target": {"ref": target, "commit": target_sha}, "settings": {"patterns": patterns, "attributes": attributes or {},
              "attribute_files": attribute_files, "uploaded_attribute_file": attribute_filename if attribute_text else None,
              "uploaded_attribute_sha256": hashlib.sha256(attribute_text.encode()).hexdigest() if attribute_text else None,
              "topic_type": kind, "guide": guide},
              "toolchain": {"adoc-dita": "0.1.0", "asciidoctor": "2.0.26", "dita-topic": "1.5.3", "dita-convert": "1.4.9", "dita_schema": "1.3-errata02"},
              "changes": [], "files": [], "notes": []}
    if base_sha == target_sha:
        report["summary"] = {"files": 0, "errors": 0, "review": 0, "xml_changed": 0}
        return report
    with tempfile.TemporaryDirectory(prefix="adoc-dita-compare-") as tmp:
        roots = [Path(tmp) / "before", Path(tmp) / "after"]
        outputs = []
        sources = []
        for label, commit, root in zip(["baseline", "target"], [base_sha, target_sha], roots):
            progress(f"Reading the {label} snapshot…")
            root.mkdir()
            snapshot(repo, commit, root)
        uploaded_path = None
        if attribute_text:
            uploaded_path = ".adoc-dita-uploaded-attributes-" + hashlib.sha256(attribute_text.encode()).hexdigest()[:16] + ".adoc"
            for root in roots:
                file = root / uploaded_path
                if file.exists() or file.is_symlink():
                    raise ValueError("Repository uses the reserved uploaded-attributes path")
                file.write_text(attribute_text, encoding="utf-8")
        selected = set(topic_paths(roots[0], patterns, kind)) | set(topic_paths(roots[1], patterns, kind))
        snapshot_attribute_files = {}
        for root in roots:
            if attribute_files is not None:
                files = list(attribute_files)
            elif guide:
                files = []
            else:
                files = ["artifacts/attributes.adoc"] if (root / "artifacts/attributes.adoc").is_file() else []
            if uploaded_path:
                files.append(uploaded_path)
            snapshot_attribute_files[root] = files
        guide_data = {}
        if guide:
            from .context import RepositoryContext, convert_repository_files
            included = set()
            for root in roots:
                files = snapshot_attribute_files[root]
                if len(files) > 1:
                    raise ValueError("Guide comparison accepts either one repository attributes file or one uploaded attributes file")
                index = RepositoryContext(root)
                inspections = index.inspect([guide], attributes, files[0] if files else None)
                included.update(o['path'] for i in inspections for o in i['occurrences'])
                guide_data[root] = (index, inspections)
            if not included:
                raise ValueError('The guide has no active topics in either snapshot. Check the guide path and conditional attributes.')
            selected &= included
        for label, root in zip(["baseline", "target"], roots):
            # A title removed in one snapshot is an invalid topic, not a deleted file.
            paths = sorted(path for path in selected if (root / path).is_file())
            # Auto-load shared definitions when present, then apply an uploaded
            # attributes file as the final source-level definition set.
            attrs_files = snapshot_attribute_files[root]
            progress(f"Converting {len(paths)} {label} topics and checking dependencies…")
            if guide:
                index, inspections = guide_data[root]
                items = convert_repository_files(root, paths, guide=guide, attributes=attributes,
                                                 attribute_files=attrs_files, kind=kind, index=index, inspections=inspections)
            else:
                items = convert_files(root, paths, attributes=attributes, attribute_files=attrs_files,
                                      kind=kind, standalone=True) if paths else []
            if uploaded_path:
                for item in items:
                    item["dependencies"] = [path for path in item.get("dependencies", []) if path != uploaded_path]
            outputs.append({item["path"]: item for item in items})
            sources.append({path: (root / path).read_text(encoding="utf-8") for path in paths})
        old, new = outputs
        progress("Building the comparison report…")
        renames = {c["after"]: c["before"] for c in changes if c["change"] == "renamed" and c["before"] in old and c["after"] in new}
        pairs = [(renames.get(path, path), path) for path in sorted(new)]
        pairs.extend((path, None) for path in sorted(set(old) - set(new) - set(renames.values())))
        handled = set()
        for before_path, after_path in pairs:
            before = old.get(before_path)
            after = new.get(after_path)
            if before is None:
                before_path = None
            dependencies = set((before or {}).get("dependencies", [])) | set((after or {}).get("dependencies", []))
            source_changed = (before_path in touched or after_path in touched)
            xml_changed = (before or {}).get("xml") != (after or {}).get("xml")
            impacted = bool(dependencies & touched)
            failed = any(side and side["status"] == "error" for side in [before, after])
            if not (source_changed or xml_changed or (impacted and failed)):
                continue
            handled.update(p for p in [before_path, after_path] if p)
            change = "added" if before is None else "deleted" if after is None else "renamed" if before_path != after_path else "modified" if source_changed else "dependency"
            old_source, new_source = sources[0].get(before_path, ""), sources[1].get(after_path, "")
            item = {"change": change, "before_path": before_path, "after_path": after_path, "before": before, "after": after,
                    "xml_changed": xml_changed, "affected_dependencies": sorted(dependencies & touched),
                    "source_diff": file_diff(old_source, new_source, before_path or "/dev/null", after_path or "/dev/null"),
                    "xml_diff": file_diff((before or {}).get("xml") or "", (after or {}).get("xml") or "", (before or {}).get("output_path", "/dev/null"), (after or {}).get("output_path", "/dev/null"))}
            # A failed conversion is unknown XML, not an XML deletion/addition.
            if any(side and side["status"] == "error" for side in [before, after]):
                item["xml_diff"] = {"patch": "", "hunks": [], "unavailable": True}
                item["xml_changed"] = None
            report["files"].append(item)
        for change in changes:
            paths = [p for p in [change["before"], change["after"]] if p and any(fnmatch.fnmatchcase(p, pattern) for pattern in patterns)]
            if paths:
                entry = dict(change)
                entry["conversion"] = "topic" if set(paths) & handled else "dependency-only or no document title"
                report["changes"].append(entry)
        report["summary"] = {"files": len(report["files"]),
                             "errors": sum(any(side and side["status"] == "error" for side in [i["before"], i["after"]]) for i in report["files"]),
                             "review": sum(any(side and side["status"] == "review" for side in [i["before"], i["after"]]) for i in report["files"]),
                             "xml_changed": sum(i["xml_changed"] is True for i in report["files"])}
        report["notes"] = ["Source and XML hunks are separate diffs, not a one-to-one source map.",
                           "Every selected topic is converted at both commits to detect include and attribute effects.",
                           "Images and other assets are referenced, not copied. Cross-file links may need review."]
    return report
