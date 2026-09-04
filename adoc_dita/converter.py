"""Parse with Asciidoctor, specialize with XSLT, validate against vendored DITA."""
from __future__ import annotations

import functools
import hashlib
import json
import os
import posixpath
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote, urlsplit, urlunsplit

from lxml import etree
from dita import convert as dita_convert

ROOT = Path(__file__).resolve().parent.parent
KINDS = {"CONCEPT": "concept", "PROCEDURE": "task", "REFERENCE": "reference"}
PARSER = lambda: etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)


@functools.lru_cache(maxsize=4)
def schema(kind):
    return etree.XMLSchema(etree.parse(str(ROOT / "schemas/dita-1.3/technicalContent/xsd" / f"{kind}.xsd"), PARSER()))


def xml_name(path):
    return str(Path(path).with_suffix(".xml")).replace(os.sep, "/")


def finalize(raw, kind="auto", references=None):
    result = {k: v for k, v in raw.items() if k != "content_type"}
    result["output_path"] = xml_name(raw["path"])
    detected = KINDS.get((raw.get("content_type") or "").upper())
    if not detected:
        prefix = Path(raw["path"]).name.split("-", 1)[0]
        detected = {"con": "concept", "proc": "task", "ref": "reference"}.get(prefix)
    result["topic_type"] = detected if kind == "auto" else kind
    diagnostics = result.setdefault("diagnostics", [])
    result["missing_attributes"] = sorted({
        match.group(1) for diagnostic in diagnostics
        if (match := re.search(r"skipping reference to missing attribute: ([\w-]+)", diagnostic["message"]))
    })
    if not result["topic_type"]:
        diagnostics.append({"severity": "error", "message": "Select Concept, Task, or Reference; or add :_mod-docs-content-type: CONCEPT, PROCEDURE, or REFERENCE to the source."})
    if not raw.get("xml") or any(d["severity"] == "error" for d in diagnostics):
        result.update(status="error", xml=None)
        return result
    try:
        root = etree.fromstring(raw["xml"].encode(), PARSER())
        if root.tag != "topic":
            raise ValueError("Converter did not return a complete DITA topic")
        transform = getattr(dita_convert, "to_" + result["topic_type"] + "_generated")
        transformed = transform(root)
        for error in transform.error_log:
            diagnostics.append({"severity": "error", "message": str(error.message)})
        root = transformed.getroot()
        # A guide's native ID registry proves which module owns a cross-topic anchor.
        targets = {}
        for ref in references or []:
            targets.setdefault(ref['id'], []).append(ref['path'])
        resolved = []
        for node in root.xpath('//*[@href]'):
            href = node.get('href')
            if href.startswith('#') and len(targets.get(href[1:], [])) == 1:
                target = targets[href[1:]][0]
                if target != raw['path']:
                    relative = posixpath.relpath(xml_name(target), posixpath.dirname(xml_name(raw['path'])) or '.')
                    node.set('href', quote(relative, safe='/') + href)
                    node.set('format', 'dita')
                    resolved.append(dict(original=href, href=node.get('href'), source_path=target, topic_id=href[1:]))
        resolved_ids = {item['original'] for item in resolved}
        diagnostics[:] = [d for d in diagnostics if not any(d['message'].endswith('Possible invalid reference: ' + ref) for ref in resolved_ids)]
        result['resolved_links'] = list({item['href']: item for item in resolved}.values())
        # The upstream backend defaults to .dita; this application deliberately emits .xml.
        for node in root.xpath("//*[@href]"):
            href = node.get("href")
            parts = urlsplit(href)
            if not parts.scheme and not parts.netloc and re.search(r"\.(adoc|asciidoc|dita)$", parts.path):
                node.set("href", urlunsplit(parts._replace(path=re.sub(r"\.(adoc|asciidoc|dita)$", ".xml", parts.path))))
        validator = schema(result["topic_type"])
        if root.tag != result["topic_type"]:
            raise ValueError("Converted root does not match the requested topic type")
        if not validator.validate(root):
            for error in validator.error_log:
                diagnostics.append({"severity": "error", "message": error.message, "xml_line": error.line})
        # Schema validity alone does not detect duplicated IDs or dangling local links.
        for paragraph in root.iter("p"):
            if "".join(paragraph.itertext()).strip() == "+":
                diagnostics.append({"severity": "warning", "message": "A paragraph contains only '+'. Check the AsciiDoc list continuation; the content was preserved."})
        ids = [n.get("id") for n in root.iter() if n.get("id")]
        if len(ids) != len(set(ids)):
            diagnostics.append({"severity": "error", "message": "Duplicate XML IDs; assign unique source IDs."})
        for node in root.xpath("//*[@href]"):
            href = node.get("href")
            if href.startswith("#") and href.split("/")[-1].lstrip("#") not in ids:
                diagnostics.append({"severity": "warning", "message": f"Unresolved local reference: {href}"})
        if any(d["severity"] == "error" for d in diagnostics):
            result.update(status="error", xml=None)
            return result
        # Sort attributes, preserve mixed-content and code whitespace. Never reflow prose.
        for element in root.iter():
            if len(element.attrib) > 1:
                attrs = sorted(element.attrib.items())
                element.attrib.clear()
                element.attrib.update(attrs)
        kind = result["topic_type"]
        doctype = f'<!DOCTYPE {kind} PUBLIC "-//OASIS//DTD DITA {kind.title()}//EN" "{kind}.dtd">'
        xml = etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True, doctype=doctype).decode()
        result.update(xml=xml, status="review" if diagnostics else "ok", sha256=hashlib.sha256(xml.encode()).hexdigest())
    except (etree.Error, ValueError) as error:
        diagnostics.append({"severity": "error", "message": str(error)})
        result.update(status="error", xml=None)
    return result


def convert_files(root, paths, *, attributes=None, attribute_files=None, kind="auto"):
    root = Path(root).resolve()
    requests = [{"root": str(root), "path": p, "attributes": attributes or {}, "attribute_files": attribute_files or []} for p in paths]
    return _convert_requests(requests, kind)


def _convert_requests(requests, kind):
    if kind not in ("auto", "concept", "task", "reference"):
        raise ValueError("Unknown topic type")
    env = dict(os.environ, BUNDLE_GEMFILE=str(ROOT / "Gemfile"), BUNDLE_PATH=str(ROOT / "vendor/bundle"))
    env.pop("RUBYOPT", None)
    process = subprocess.run(["bundle", "exec", "ruby", str(ROOT / "scripts/convert.rb")],
                             input=json.dumps(requests), text=True, capture_output=True, env=env,
                             cwd=ROOT, timeout=max(60, len(requests) * 5))
    if process.returncode:
        raise RuntimeError("Converter could not start. Run ./scripts/setup.sh.\n" + process.stderr[-3000:])
    return [finalize(item, kind, request.get('references')) for item, request in zip(json.loads(process.stdout), requests)]


def convert_text(text, *, filename="document.adoc", attributes=None, kind="auto", attribute_text="", attribute_file=None):
    if Path(filename).name != filename or not filename.lower().endswith(".adoc"):
        raise ValueError("Use a filename ending in .adoc without directories")
    if attribute_file:
        if attribute_text.strip():
            raise ValueError("Choose an attributes file or inline attribute text, not both")
        path = Path(attribute_file).expanduser()
        if not path.is_absolute():
            raise ValueError("Enter the full absolute path to your attributes .adoc file")
        if path.suffix.lower() != ".adoc":
            raise ValueError("Choose an attributes file ending in .adoc")
        if not path.is_file():
            raise ValueError(f"Attributes file not found: {path}. Check the location on this computer.")
        path = path.resolve()
        # Pass the pasted source in memory. Never create or overwrite files in the clone.
        # Native attribute includes retain their directory and stay inside this root.
        request = {"root": str(path.parent), "path": filename, "source": text,
                   "attributes": attributes or {}, "attribute_files": [path.name]}
        return _convert_requests([request], kind)[0]
    with tempfile.TemporaryDirectory(prefix="adoc-dita-") as folder:
        root = Path(folder)
        (root / filename).write_text(text, encoding="utf-8")
        files = []
        if attribute_text.strip():
            (root / "_attributes.adoc").write_text(attribute_text, encoding="utf-8")
            files.append("_attributes.adoc")
        return convert_files(root, [filename], attributes=attributes, attribute_files=files, kind=kind)[0]
