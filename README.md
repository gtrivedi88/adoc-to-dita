# AsciiDoc → DITA

A local tool for two jobs:

1. Paste or upload AsciiDoc and copy/download validated DITA XML.
2. Compare two Git branches, tags, or commits and download the affected topics, before/after XML, and readable diffs.

Supports **concept**, **task (procedure)**, and **reference** topics. No Vale, AI service, AEM connection, account, or database is required. The browser interface runs on your computer; GitHub access is only used when you choose a remote repository.

## Start

Prerequisites: Git, Python 3.10+, Ruby 3.2+, and Bundler. macOS and Linux are supported; use WSL on Windows.

```sh
git clone https://github.com/gtrivedi88/adoc-to-dita.git
cd adoc-dita
./scripts/setup.sh
./adoc-dita serve
```

Open **http://127.0.0.1:8765**. Setup downloads pinned dependencies. Subsequent standalone conversions and comparisons of locally available Git objects work offline.

### Convert content

Paste the complete module, or upload an `.adoc` file. Select a type, or use **Detect from content**:

| AsciiDoc metadata | Output |
| --- | --- |
| `:_mod-docs-content-type: CONCEPT` | `<concept>` with `<conbody>` |
| `:_mod-docs-content-type: PROCEDURE` | `<task>` with `<taskbody>` |
| `:_mod-docs-content-type: REFERENCE` | `<reference>` with `<refbody>` |

If metadata is absent, filenames starting with `con-`, `proc-`, and `ref-` identify the type. Otherwise, select the type explicitly. The tool does not infer a document's meaning from its prose. `[role="_abstract"]` marks the short description. Procedures use `.Prerequisites`, `.Procedure`, and `.Verification` blocks.

The `examples/` directory includes representative modules, the supplied reference XML, and the RBAC concept and LDAP task source modules. `reference.adoc` demonstrates the supplied XML vocabulary; it is a shorter illustrative source, not a reconstruction of the entire supplied file.

The browser conversion form requires only the AsciiDoc content. An attributes file is optional: choose the `.adoc` file directly or enter its full local path. It loads shared definitions directly; it never activates a project or guide selector. If a supplied path belongs to a local Git clone and the pasted topic has one unambiguous source match, the source path is used quietly as the base for local snippets, includes, and topic links. The pasted and uploaded content remains in memory and the clone is never modified.

Many modular documentation sources use an ID such as `[id="configuration_{context}"]`, where an assembly normally supplies `context`. Standalone conversion deterministically emits the base ID `configuration`. The same rule applies to structural xref targets. Set `context=admin-guide` under **Attribute overrides** when the desired output is `configuration_admin-guide`. Uses of `{context}` in prose are still reported as missing; content attributes are never guessed.

This workflow is generic. It contains no hard-coded RHDH contexts, guide names, products, or directory layout. The `:_mod-docs-content-type:` convention is supported for modular documentation, and explicit topic-type selection supports repositories that use other conventions.

**Attribute overrides** accepts `name=value`, one per line, and takes precedence over file definitions. Use these to choose a build variant or provide values normally supplied by an external build configuration:

```text
product=Red Hat Developer Hub
product-short=Developer Hub
context=standalone
```

Without an attributes file, pasted and uploaded files are self-contained. An attributes file outside a Git clone is read directly, with includes confined to its folder. Missing attributes are listed once in the UI; unsupported content produces diagnostics instead of silently disappearing. Existing explicit IDs are preserved. Files without an ID receive a stable ID derived from their logical source path.

The CLI retains an advanced `--repository --guide` mode for builds that explicitly need inherited guide attributes and a publication-wide link registry. It is separate from the browser’s paste workflow. The converter does not run repository scripts or infer externally supplied build flags.

### Compare releases

Enter a public GitHub repository URL or a local Git repository path, then select two branch names, tags, or commit IDs. **Load releases** populates suggestions. Local repositories may use `origin/release-1.9` when that branch has not been checked out locally. Local repositories are not fetched automatically; run `git fetch` yourself when needed.

Choose your repository and topic paths. Patterns use Python `fnmatch`: `*` also matches subdirectories. The default `*.adoc` selects every AsciiDoc path; narrow it to your topics folder or enter several comma-separated patterns. The scope is individual typed modules, not DITA map or book generation.

The comparison is **baseline snapshot → target snapshot**, not a pull-request merge-base comparison. The selected refs are resolved to commit IDs and recorded in the report. Your checkout and branches are never switched or modified.

Every selected topic is converted at both commits. This catches changes caused by included snippets, code files, conditions, and shared attributes even when a topic's own source is unchanged. Topics with identical source and XML are omitted. A changed source file with unchanged XML (for example a comment edit) remains in the report.

Set **Guide entry file** to resolve attributes and topic links independently from the guide in **each snapshot**. Only matching topics included by that guide in either snapshot are compared. A topic with multiple active inclusions must be converted individually with an inclusion selection; a missing inclusion is reported as unavailable rather than silently choosing a context. Without a guide, `artifacts/attributes.adoc` is automatically loaded when present, and additional attributes may need to be supplied. Settings also accept other attributes files relative to the repository root. Explicit overrides take precedence over source definitions.

Download the ZIP and open `report.html`, or inspect `report.json`:

```text
report.html                 Readable comparison
report.json                 Commit IDs, settings, diagnostics, line ranges
before/modules/example.xml  Valid baseline XML, when available
after/modules/example.xml   Valid target XML, when available
diffs/...source.diff        AsciiDoc unified diff
diffs/...xml.diff           XML unified diff
```

Additions, deletions, and Git-detected renames are reported. Deleted topics only have baseline XML. A conversion failure is explicitly marked as unavailable; it is never represented as an XML deletion. Changed snippets and files without a title are listed as dependencies/non-topics rather than emitted as invalid standalone documents.

**Source and XML hunks are separate diffs, not a one-to-one source map.** Their ranges refer to the exact files in this run. Inserts and deletions have a zero-length side with an `after_line` insertion point. AEM editor line numbers and synchronization are outside Phase 1.

## CLI

```sh
# Convert a complete module; write XML to stdout by default.
./adoc-dita convert examples/reference.adoc -o results/reference.xml

# Paste through stdin and explicitly select a topic type.
printf '= Introduction\n\nHello *world*.\n' | ./adoc-dita convert --type concept

# Paste through stdin and load the same attributes file used by the browser.
cat modules/con-application-configuration-file.adoc | ./adoc-dita convert - \
  --filename con-application-configuration-file.adoc \
  --attribute-file /path/to/docs/artifacts/attributes.adoc \
  -o results/application-configuration-file.xml

# Resolve local includes and attribute files inside a chosen root.
./adoc-dita convert examples/proc-share-a-secret-with-ldap.adoc \
  --root examples --attribute-file attributes.adoc -o results/ldap.xml

# Compare a local documentation repository.
./adoc-dita compare /path/to/rhdh \
  --base origin/release-1.9 --target origin/release-1.10 \
  --include 'modules/*.adoc' -a context=standalone \
  -o results/release-1.9-to-1.10

# Use any repository's guide entry document for a single topic or a release diff.
./adoc-dita convert /path/to/docs/topics/configuration.adoc \
  --repository /path/to/docs --guide guides/admin.adoc --type concept
./adoc-dita compare /path/to/docs --base v1 --target v2 \
  --guide guides/admin.adoc --include 'topics/*.adoc' --type concept \
  -o results/admin-v1-to-v2

# Or compare public GitHub branches directly.
./adoc-dita compare \
  https://github.com/redhat-developer/red-hat-developers-documentation-rhdh \
  --base release-1.9 --target release-1.10 \
  --include 'modules/*.adoc' -a context=standalone \
  -o results/github-comparison
```

Use a new/empty comparison output directory to avoid mixing runs. `convert --json` returns XML and diagnostics together. Exit status: `0` successful (possibly with review warnings), `1` invocation/Git/setup error, `2` one or more conversion failures. Comparisons still save successful XML and diagnostics when some topics fail.

## Supported content and limits

See [SUPPORTED.md](SUPPORTED.md) for the audited element matrix, including the supplied plugin topic and map files.

- Paragraphs, emphasis, inline code, external links, lists and definition lists, tables, images, code blocks, notes/warnings, short descriptions, and procedure sections use the upstream conversion rules.
- Native Asciidoctor handles relative includes, include tags/line ranges, attributes, and conditional content. Include paths resolve relative to the current file first, then relative to the selected input root (for modular paths such as `artifacts/snippet.adoc`). Includes stay inside the selected input root. Remote includes and symlinks outside the root are rejected. Git snapshots preserve internal include aliases and omit symlinks that leave the snapshot.
- Nested section structures and other constructs that cannot be converted safely return an error. There is no claim that arbitrary AsciiDoc can always become valid specialized DITA.
- Converter warnings that indicate possible content loss block output. Cross-reference warnings and standalone `+` paragraphs remain available for review. The application never removes a literal `+` automatically.
- All successful XML is validated against vendored **DITA 1.3 plus Errata 02** XSDs, with additional ID checks. Validation is structural; it does not verify technical correctness, external URLs, or whether referenced image assets exist.
- Local topic references are changed to `.xml`. Cross-file references with fragments may need review because a complete publication-wide ID/key registry is not part of this phase.
- Images are referenced, not uploaded or copied. DITA map/bookmap generation, AEM metadata and identity, publishing, and content synchronization are deferred.
- Browser input is limited to 2 MB. Remote access supports HTTPS GitHub repositories; for private repositories, use an authenticated local clone. GitHub snapshots are cached in `.cache/repos/` and can be removed when the application is stopped.

## Determinism

With the same source snapshots, attributes, conversion settings, and runtime/toolchain, XML and report ZIPs are reproducible. Dependencies are pinned; paths and attributes are ordered; generated IDs are stable; ZIP entry timestamps are fixed. No LLM, random topic IDs, or build timestamps are used. Asciidoctor's implicit date/time attributes are fixed to the Unix epoch; supply explicit release dates as your own source attributes.

Whitespace in code blocks and mixed XML content is preserved. Formatting may differ from hand-authored XML. The goal is equivalent valid content, not byte-for-byte reproduction of previously migrated XML.

## Development and validation

```sh
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

Tests exercise the CLI as subprocesses for file, stdin, and branch-comparison workflows, plus all three topic types, the supplied plugin-topic vocabulary, standalone context IDs, automatic local snippet resolution, the LDAP task and RBAC concept, escaping, code whitespace, missing attributes, include confinement, tagged/conditional includes, type selection, additions/deletions/renames, dependency changes, invalid output handling, and repeatable XML/ZIP results. An inactive GitHub Actions template is provided at `ci/github-actions.yml`. To enable automatic tests on pushes and pull requests, use a GitHub login with workflow permission and copy it to `.github/workflows/test.yml`.

Implementation: a Python CLI/local HTTP interface, a JSON bridge to the pinned `asciidoctor-dita-topic` Ruby converter, `dita-convert` XSLT specialization, and lxml schema validation. See [THIRD_PARTY.md](THIRD_PARTY.md) for upstream tools, schemas, and source attribution.
