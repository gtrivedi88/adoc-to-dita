"""Locate modules and inspect inherited attributes through native guide parsing."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .converter import ROOT, _convert_requests, convert_text, standalone_source, xml_name

EXCLUDED = {'.git', '.cache', '.venv', 'vendor', 'node_modules'}
# These configure the parser/output, rather than the topic's inherited content.
RESERVED = {'doctype', 'backend', 'leveloffset', 'docfile', 'docdir', 'docname', 'outfilesuffix',
            'allow-uri-read', 'max-include-depth', 'attribute-missing'}


class SourceNotFoundError(ValueError):
    pass


def confined(root, path):
    file = (root / path).resolve()
    if not file.is_relative_to(root):
        raise ValueError('Path must stay inside the repository')
    return file


def identity(source):
    # This index only finds candidates. Asciidoctor resolves attributes and includes.
    header = source.split('\n= ', 1)[0]
    anchor = re.search(r'^\[id=["\']([^"\']+)["\']\]', header, re.M) or re.search(r'^\[\[([^,\]]+)', header, re.M)
    title = re.search(r'^= (.+)$', source, re.M)
    return (anchor.group(1) if anchor else None, title.group(1) if title else None)


class RepositoryContext:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError('Local repository folder does not exist')
        self.sources = {}
        self.topics = {}
        self.parents = {}
        self.attribute_names = set()
        typed = set()
        for folder, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (Path(folder) / d).is_symlink())
            for name in sorted(files):
                file = Path(folder) / name
                if file.suffix != '.adoc' or file.is_symlink():
                    continue
                path = file.relative_to(self.root).as_posix()
                source = file.read_text(encoding='utf-8')
                self.sources[path] = source
                title = re.search(r'^= .+$', source, re.M)
                metadata = re.search(r'^:_(?:mod-docs-content-type|content-type|module-type):\s*(\w+)', source, re.M)
                content_type = metadata.group(1).upper() if metadata else ''
                if content_type in ('CONCEPT', 'PROCEDURE', 'REFERENCE') or name.startswith(('con-', 'proc-', 'ref-')):
                    typed.add(path)
                if title and content_type not in ('ASSEMBLY', 'MAP', 'SNIPPET', 'ATTRIBUTES'):
                    self.topics[path] = {'path': path, 'title_line': source[:title.start()].count('\n') + 1}
                self.attribute_names.update(re.findall(r'(?:^|\[):([\w-]+):', source, re.M))
        self.attribute_names = sorted(n for n in self.attribute_names if n not in RESERVED and not n.startswith(('_mod-docs', '_content-type', '_module-type')))
        for parent, source in self.sources.items():
            for target in re.findall(r'^include::([^\[]+)\[', source, re.M):
                if '{' in target:
                    continue
                for candidate in [self.root / Path(parent).parent / target, self.root / target]:
                    resolved = candidate.resolve()
                    if resolved.is_relative_to(self.root) and resolved.is_file():
                        child = resolved.relative_to(self.root).as_posix()
                        self.parents.setdefault(child, set()).add(parent)
                        break
        # Untyped documents containing other titled documents are guide candidates.
        # Leaf documents can use explicit topic-type selection instead of any naming convention.
        containers = {parent for child, parents in self.parents.items() if identity(self.sources.get(child, ''))[1] for parent in parents}
        self.topics = {p: topic for p, topic in self.topics.items() if p in typed or p not in containers}

    def match(self, source, filename, source_path=None):
        if source_path:
            path = confined(self.root, source_path).relative_to(self.root).as_posix()
            if path not in self.topics:
                raise ValueError('Select a topic .adoc file inside the repository')
            return [path]
        anchor, title = identity(source)
        if anchor:
            matches = [p for p in self.topics if identity(self.sources[p])[0] == anchor]
            if matches:
                return sorted(matches)
        matches = [p for p in self.topics if Path(p).name == filename] if filename != 'document.adoc' else []
        if not matches and title:
            matches = [p for p in self.topics if identity(self.sources[p])[1] == title]
        return sorted(matches)

    def guides(self, path):
        # Attribute-expanded include paths can introduce additional parent guides.
        # Treat their entry documents as candidates even when static parents exist.
        dynamic = [p for p, text in self.sources.items() if re.search(r'^include::[^\n\[]*\{', text, re.M)]
        seen, pending = set(), list(self.parents.get(path, [])) + dynamic
        while pending:
            parent = pending.pop()
            if parent not in seen:
                seen.add(parent)
                pending.extend(self.parents.get(parent, []))
        roots = sorted(p for p in seen if not self.parents.get(p) and identity(self.sources[p])[1])
        # Dynamic include filenames cannot be proven by the candidate index.
        # Let the native parser check entry documents when no static parent was found.
        containers = {parent for parents in self.parents.values() for parent in parents}
        return roots or sorted(p for p, text in self.sources.items() if not self.parents.get(p) and identity(text)[1]
                               and (p in containers or re.search(r'^include::', text, re.M)))

    def inspect(self, guides, attributes=None, attribute_file=None):
        requests = [dict(root=str(self.root), guide=guide, topics=list(self.topics.values()),
                         attribute_names=self.attribute_names, attributes=attributes or {},
                         attribute_file=attribute_file) for guide in guides]
        env = dict(os.environ, BUNDLE_GEMFILE=str(ROOT / 'Gemfile'), BUNDLE_PATH=str(ROOT / 'vendor/bundle'))
        env.pop('RUBYOPT', None)
        process = subprocess.run(['bundle', 'exec', 'ruby', str(ROOT / 'scripts/repository_context.rb')],
                                 input=json.dumps(requests), capture_output=True, text=True, cwd=ROOT,
                                 env=env, timeout=max(60, len(guides) * 10))
        if process.returncode:
            raise RuntimeError('Could not inspect repository guides: ' + process.stderr[-2000:])
        return json.loads(process.stdout)


def infer_repository(attribute_file):
    if not attribute_file:
        return None
    file = Path(attribute_file).expanduser().resolve()
    if not file.is_file():
        raise ValueError(f'Attributes file not found: {file}')
    for folder in file.parents:
        if (folder / '.git').exists():
            return folder
    return None


def convert_standalone(source, *, filename='document.adoc', attributes=None, kind='auto',
                       attribute_file=None, attribute_text=''):
    """Convert pasted content without asking the user to select a guide.

    When the attributes file belongs to a Git clone, a unique source match is
    used only as the local include base. No guide, project, or inherited context
    is selected. If no unique match exists, normal attributes-file conversion is
    used and no ambiguity is exposed to the user.
    """
    if attribute_file and attribute_text.strip():
        raise ValueError('Choose an attributes file upload or enter its local path, not both')
    if attribute_text.strip():
        return convert_text(source, filename=filename, attributes=attributes, kind=kind,
                            attribute_text=attribute_text)
    repository = infer_repository(attribute_file)
    if repository:
        index = RepositoryContext(repository)
        paths = index.match(source, filename)
        if len(paths) == 1:
            attribute_path = confined(index.root, Path(attribute_file).expanduser())
            prepared, normalized = standalone_source(source, attributes)
            path = paths[0]
            # Resolve explicit cross-topic IDs against unique standalone topic
            # IDs. A source target can contain a historical guide suffix, such
            # as ``install_admin-guide``; the standalone target is ``install``.
            bases = {}
            for candidate, candidate_source in index.sources.items():
                anchor, _ = identity(candidate_source)
                if candidate not in index.topics or not anchor:
                    continue
                base = anchor.replace('_{context}', '').replace('-{context}', '')
                bases.setdefault(base, []).append(candidate)
            targets = set(re.findall(r'\bxref:([^\[\s]+)', prepared))
            targets.update(re.findall(r'<<([^,>\s]+)', prepared))
            references = []
            for target in sorted(targets):
                target = target.lstrip('#')
                candidates = [(base, files) for base, files in bases.items()
                              if target == base or target.startswith(base + '_') or target.startswith(base + '-')]
                if not candidates:
                    continue
                longest = max(len(base) for base, _ in candidates)
                best = [(base, files) for base, files in candidates if len(base) == longest]
                if len(best) == 1 and len(best[0][1]) == 1:
                    references.append(dict(id=target, path=best[0][1][0], target_id=best[0][0]))
            request = dict(root=str(index.root), path=path, source=prepared,
                           attributes=attributes or {},
                           attribute_files=[attribute_path.relative_to(index.root).as_posix()],
                           references=references)
            result = _convert_requests([request], kind)[0]
            result['source_path'] = path
            result['source_match'] = 'automatic'
            if normalized:
                result['standalone_context'] = 'base-id'
            return result
    return convert_text(source, filename=filename, attributes=attributes, kind=kind,
                        attribute_file=attribute_file, attribute_text=attribute_text)


def convert_in_repository(source, *, repository, filename='document.adoc', source_path=None,
                          guide=None, profile=None, attributes=None, kind='auto', attribute_file=None,
                          _index=None, _inspections=None):
    index = _index or RepositoryContext(repository)
    if attribute_file:
        file = confined(index.root, Path(attribute_file).expanduser())
        if not file.is_file() or file.suffix != '.adoc':
            raise ValueError('Select an existing attributes .adoc file inside this repository')
        attribute_file = file.relative_to(index.root).as_posix()
    paths = index.match(source, filename, source_path)
    base = dict(path=filename, output_path=xml_name(filename), xml=None, topic_type=None,
                missing_attributes=[], dependencies=[], diagnostics=[], status='selection',
                repository=str(index.root), source_choices=paths, guide_choices=[])
    if not paths:
        raise SourceNotFoundError('Could not match this content to a repository topic. Supply its repository-relative Source file path, or clear the repository field for standalone conversion.')
    if len(paths) > 1:
        base['selection_message'] = 'This content matches multiple source files. Choose the file to convert.'
        return base
    path = paths[0]
    guides = [confined(index.root, guide).relative_to(index.root).as_posix()] if guide else index.guides(path)
    inspections = _inspections if _inspections is not None else index.inspect(guides, attributes, attribute_file)
    choices = []
    for inspection in inspections:
        for occurrence in inspection['occurrences']:
            if occurrence['path'] != path:
                continue
            key = hashlib.sha256(json.dumps([inspection['guide'], occurrence], sort_keys=True).encode()).hexdigest()[:20]
            choices.append(dict(key=key, guide=inspection['guide'], title=inspection['title'],
                                context=occurrence['attributes'].get('context', ''),
                                parent=occurrence['parent'], line=occurrence['line'],
                                topic_id=occurrence['id'], occurrence=occurrence, inspection=inspection))
    base.update(source_path=path, path=path, output_path=xml_name(path),
                guide_choices=[{k: v for k, v in choice.items() if k not in ('occurrence', 'inspection')} for choice in choices])
    selected = next((c for c in choices if c['key'] == profile), None)
    if not selected and len(choices) == 1:
        selected = choices[0]
    if not selected:
        base['selection_message'] = 'This topic is used in multiple guides. Choose its guide to load the inherited attributes.' if choices else 'No active inclusion was found. Select a guide path or check its include conditions.'
        base['diagnostics'] = [dict(severity='warning', message=f"{i['guide']}: {d['message']}") for i in inspections for d in i['diagnostics'] if d['severity'] in ('error', 'fatal')]
        return base
    inherited = selected['occurrence']['attributes']
    soft_attributes = {key + '@': value for key, value in inherited.items()}
    soft_attributes.update(attributes or {})
    request = dict(root=str(index.root), path=path, source=source, attributes=soft_attributes,
                   references=selected['inspection']['references'], use_header_title=True)
    result = _convert_requests([request], kind)[0]
    result.update(repository=str(index.root), source_path=path,
                  resolution={k: v for k, v in selected.items() if k not in ('occurrence', 'inspection')},
                  guide_choices=base['guide_choices'])
    result['dependencies'] = sorted(set(result['dependencies']) | set(selected['inspection']['dependencies']) | {selected['guide']})
    return result


def convert_repository_files(root, paths, *, guide, attributes=None, attribute_files=None, kind='auto', index=None, inspections=None):
    index = index or RepositoryContext(root)
    if attribute_files and len(attribute_files) > 1:
        raise ValueError('Guide conversion accepts one shared attributes file; additional files can be included by that file or the guide')
    shared = attribute_files[0] if attribute_files else None
    inspections = inspections if inspections is not None else index.inspect([guide], attributes, shared)
    results = []
    for path in paths:
        try:
            result = convert_in_repository((index.root / path).read_text(encoding='utf-8'), repository=root,
                                           source_path=path, filename=Path(path).name, guide=guide,
                                           attributes=attributes, attribute_file=shared, kind=kind,
                                           _index=index, _inspections=inspections)
            if result['status'] == 'selection':
                result['status'] = 'error'
                result['diagnostics'].append(dict(severity='error', message=result['selection_message']))
        except ValueError as error:
            result = dict(path=path, output_path=xml_name(path), xml=None, status='error',
                          dependencies=[guide], diagnostics=[dict(severity='error', message=str(error))])
        # Reports must not contain temporary extraction locations.
        result.pop('repository', None)
        results.append(result)
    return results
