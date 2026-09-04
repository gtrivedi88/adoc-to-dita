"""Stable diffs and portable output bundles."""
import difflib
import html
import io
import json
from pathlib import Path
import zipfile


def file_diff(before, after, old_name, new_name):
    a, b = before.splitlines(), after.splitlines()
    hunks = []
    for action, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if action == "equal":
            continue
        hunks.append({"action": action,
                      "before": {"start": i + 1 if j > i else None, "end": j if j > i else None, "count": j - i, "after_line": i},
                      "after": {"start": k + 1 if l > k else None, "end": l if l > k else None, "count": l - k, "after_line": k}})
    patch = "\n".join(difflib.unified_diff(a, b, fromfile=old_name, tofile=new_name, lineterm=""))
    return {"patch": patch + ("\n" if patch else ""), "hunks": hunks}


def report_html(report):
    esc = html.escape
    cards = []
    for item in report["files"]:
        name = item["after_path"] or item["before_path"]
        side = item["after"] or item["before"]
        diagnostics = []
        for label in ["before", "after"]:
            if item[label]:
                diagnostics.extend(f'{label}: {d["severity"]}: {d["message"]}' for d in item[label]["diagnostics"])
        links = []
        for label in ["before", "after"]:
            if item[label] and item[label].get("xml"):
                path = label + "/" + item[label]["output_path"]
                from urllib.parse import quote
                links.append(f'<a href="{quote(path)}">{label.title()} XML</a>')
        cards.append(f'<article><h2>{esc(name)}</h2><p class="badge">{esc(item["change"])} · {esc(side["status"])}</p>'
                     f'<p>{" · ".join(links)}</p><p>{esc("; ".join(diagnostics))}</p>'
                     f'<h3>AsciiDoc changes</h3><pre>{esc(item["source_diff"]["patch"] or "No direct source change.")}</pre>'
                     f'<h3>XML changes</h3><pre>{esc(item["xml_diff"]["patch"] or ("Unavailable: conversion failed." if item["xml_diff"].get("unavailable") else "No XML change."))}</pre></article>')
    notes = " ".join(report.get("notes", []))
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AsciiDoc → DITA comparison</title><style>
body{{font:16px system-ui,sans-serif;background:#f4f5f7;color:#17202e;max-width:1100px;margin:40px auto;padding:0 24px}}h1{{font-size:30px}}article{{background:white;border:1px solid #d8dce3;border-radius:12px;padding:24px;margin:20px 0}}pre{{overflow:auto;background:#f5f6f8;padding:16px;font:13px/1.6 monospace;white-space:pre}}a{{color:#205bc8}}.badge{{color:#476071}}h2{{font-size:19px;overflow-wrap:anywhere}}
</style><h1>AsciiDoc → DITA</h1><p>{esc(report['base']['ref'])} → {esc(report['target']['ref'])}</p>
<p>{esc(report['base']['commit'])}<br>{esc(report['target']['commit'])}</p><p>{report['summary']['files']} affected topics · {report['summary']['errors']} with conversion errors</p>
<p>{esc(notes)}</p>{''.join(cards) or '<article>No affected topics.</article>'}<p>See report.json for exact line ranges and all changed files.</p></html>'''


def bundle_files(report):
    files = {"report.json": json.dumps(report, indent=2, ensure_ascii=False) + "\n", "report.html": report_html(report)}
    for item in report["files"]:
        for side in ["before", "after"]:
            converted = item[side]
            if converted and converted.get("xml"):
                files[side + "/" + converted["output_path"]] = converted["xml"]
        name = item["after_path"] or item["before_path"]
        files["diffs/" + name + ".source.diff"] = item["source_diff"]["patch"]
        files["diffs/" + name + ".xml.diff"] = item["xml_diff"]["patch"]
    return files


def zip_report(report):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(bundle_files(report).items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content.encode())
    return stream.getvalue()


def save_report(report, output):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be empty; choose a new directory to avoid mixing runs")
    output.mkdir(parents=True, exist_ok=True)
    for path, content in bundle_files(report).items():
        file = output / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
    (output / "comparison.zip").write_bytes(zip_report(report))
