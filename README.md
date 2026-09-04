# AsciiDoc → DITA

A local tool for two jobs:

1. Paste or upload AsciiDoc and copy/download validated DITA XML.
2. Compare two Git branches, tags, or commits and download the affected topics, before/after XML, and readable diffs.

Supports **concept**, **task (procedure)**, and **reference** topics. No Vale, AI service, AEM connection, account, or database is required. The browser interface runs on your computer; GitHub access is only used when you choose a remote repository.

## Start

Prerequisites: Git, Python 3.10+, Ruby 3.2+, and Bundler. macOS and Linux are supported; use WSL on Windows.

```sh
git clone https://github.com/gtrivedi88/adoc-dita.git
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

For a module from a local clone, enter the full path to its `.adoc` attributes file in **Attributes file location**, for example `/path/to/your-clone/artifacts/attributes.adoc`. The browser remembers the path, and the converter reads the file on every conversion. It never writes into your clone. Attribute references, conditions, and nested includes are handled by Asciidoctor; includes must stay within the selected attributes file's folder.

**Attribute overrides** accepts `name=value`, one per line, and takes precedence over file definitions. Shared attribute files often omit guide-specific values such as `context`; use the value defined by the module's containing guide or assembly to preserve its ID. For example:

```text
product=Red Hat Developer Hub
product-short=Developer Hub
context=standalone
```

Without an attributes file, pasted and uploaded files are self-contained. With one, includes resolve relative to its folder; for source modules with includes elsewhere in the repository, use the CLI with `--root` or a repository comparison. Missing attributes are listed once in the UI with instructions to supply them; unsupported content produces diagnostics instead of silently disappearing. Existing explicit IDs are preserved. Files without an ID receive a stable ID derived from their logical source path.

### Compare releases

Enter a public GitHub repository URL or a local Git repository path, then select two branch names, tags, or commit IDs. **Load releases** populates suggestions. Local repositories may use `origin/release-1.9` when that branch has not been checked out locally. Local repositories are not fetched automatically; run `git fetch` yourself when needed.

The interface defaults to the RHDH repository and `modules/*.adoc`. Patterns use Python `fnmatch`: `*` also matches subdirectories. Use `*.adoc` for every AsciiDoc path, or enter several comma-separated patterns. The scope is individual typed modules, not DITA map or book generation.

The comparison is **baseline snapshot → target snapshot**, not a pull-request merge-base comparison. The selected refs are resolved to commit IDs and recorded in the report. Your checkout and branches are never switched or modified.

Every selected topic is converted at both commits. This catches changes caused by included snippets, code files, conditions, and shared attributes even when a topic's own source is unchanged. Topics with identical source and XML are omitted. A changed source file with unchanged XML (for example a comment edit) remains in the report.

When present, `artifacts/attributes.adoc` is automatically loaded from **each snapshot**. Additional guide-level attributes such as `context` or `a-platform-generic` may need to be supplied. The advanced settings let you specify different attribute files relative to the repository root. CLI attributes take precedence over file definitions.

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

# Resolve local includes and attribute files inside a chosen root.
./adoc-dita convert examples/proc-share-a-secret-with-ldap.adoc \
  --root examples --attribute-file attributes.adoc -o results/ldap.xml

# Compare a local documentation repository.
./adoc-dita compare /path/to/rhdh \
  --base origin/release-1.9 --target origin/release-1.10 \
  --include 'modules/*.adoc' -a context=standalone \
  -o results/release-1.9-to-1.10

# Or compare public GitHub branches directly.
./adoc-dita compare \
  https://github.com/redhat-developer/red-hat-developers-documentation-rhdh \
  --base release-1.9 --target release-1.10 \
  --include 'modules/*.adoc' -a context=standalone \
  -o results/github-comparison
```

Use a new/empty comparison output directory to avoid mixing runs. `convert --json` returns XML and diagnostics together. Exit status: `0` successful (possibly with review warnings), `1` invocation/Git/setup error, `2` one or more conversion failures. Comparisons still save successful XML and diagnostics when some topics fail.

## Supported content and limits

- Paragraphs, emphasis, inline code, external links, lists and definition lists, tables, images, code blocks, notes/warnings, short descriptions, and procedure sections use the upstream conversion rules.
- Native Asciidoctor handles relative includes, include tags/line ranges, attributes, and conditional content. Include paths resolve relative to the current file first, then relative to the selected input root (for modular paths such as `artifacts/snippet.adoc`). Includes stay inside the selected input root. Remote includes and symlinks outside the root are rejected; Git snapshot extraction does not materialize symlinks.
- Nested section structures and other constructs that cannot be converted safely return an error. There is no claim that arbitrary AsciiDoc can always become valid specialized DITA.
- Converter warnings that indicate possible content loss block output. Cross-reference warnings and standalone `+` paragraphs remain available for review. The application never removes a literal `+` automatically.
- All successful XML is validated against vendored **DITA 1.3 plus Errata 02** XSDs, with additional ID checks. Validation is structural; it does not verify technical correctness, external URLs, or whether referenced image assets exist.
- Local topic references are changed to `.xml`. Cross-file references with fragments may need review because a complete publication-wide ID/key registry is not part of this phase.
- Images are referenced, not uploaded or copied. Assemblies/maps, AEM metadata and identity, publishing, and content synchronization are deferred.
- Browser input is limited to 2 MB. Remote access supports HTTPS GitHub repositories; for private repositories, use an authenticated local clone. GitHub snapshots are cached in `.cache/repos/` and can be removed when the application is stopped.

## Determinism

With the same source snapshots, attributes, conversion settings, and runtime/toolchain, XML and report ZIPs are reproducible. Dependencies are pinned; paths and attributes are ordered; generated IDs are stable; ZIP entry timestamps are fixed. No LLM, random topic IDs, or build timestamps are used. Asciidoctor's implicit date/time attributes are fixed to the Unix epoch; supply explicit release dates as your own source attributes.

Whitespace in code blocks and mixed XML content is preserved. Formatting may differ from hand-authored XML. The goal is equivalent valid content, not byte-for-byte reproduction of previously migrated XML.

## Development and validation

```sh
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

Tests exercise all three topic types, the supplied reference vocabulary, the LDAP task and RBAC concept, escaping, code whitespace, missing attributes, include confinement, tagged/conditional includes, type selection, additions/deletions/renames, dependency changes, invalid output handling, and repeatable XML/ZIP results. An inactive GitHub Actions template is provided at `ci/github-actions.yml`. To enable automatic tests on pushes and pull requests, use a GitHub login with workflow permission and copy it to `.github/workflows/test.yml`.

Implementation: a Python CLI/local HTTP interface, a JSON bridge to the pinned `asciidoctor-dita-topic` Ruby converter, `dita-convert` XSLT specialization, and lxml schema validation. See [THIRD_PARTY.md](THIRD_PARTY.md) for upstream tools, schemas, and source attribution.
