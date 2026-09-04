# frozen_string_literal: true
# A narrow JSON bridge to the pinned, upstream Asciidoctor DITA converter.
require 'json'
require 'asciidoctor'
require 'dita-topic'
require 'pathname'
require 'digest'

# Keep Asciidoctor's native conditional, tagged and line-filtered includes.
# Check paths immediately before its own file reader opens them.
module ConfinedIncludes
  def resolve_include_path(target, attrlist, attrs)
    raise "Remote includes are not supported: #{target}" if target.match?(/\A[a-z][a-z0-9+.-]*:/i)
    root = Thread.current[:conversion_root]
    candidate = File.expand_path(target, @dir)
    # Modular repositories also use root-relative includes such as artifacts/foo.adoc.
    # An existing path relative to the current module always takes precedence.
    fallback = File.expand_path(target, root)
    if !File.exist?(candidate) && !Pathname.new(target).absolute? && File.file?(fallback)
      target = candidate = fallback
    end
    candidate = File.realpath(candidate) if File.exist?(candidate)
    unless candidate.start_with?(root + File::SEPARATOR)
      raise "Include is outside the input root: #{target}"
    end
    Thread.current[:dependencies] << Pathname.new(candidate).relative_path_from(Pathname.new(root)).to_s
    super
  end
end
Asciidoctor::PreprocessorReader.prepend ConfinedIncludes

def convert_one(request)
  root = File.realpath(request.fetch('root'))
  path = request.fetch('path')
  file = File.expand_path(path, root)
  raise 'Input path must stay within the input root' unless file.start_with?(root + File::SEPARATOR)
  unless request.key?('source')
    raise 'Input file is a symlink outside the input root' unless File.realpath(file).start_with?(root + File::SEPARATOR)
  end
  Thread.current[:conversion_root] = root
  Thread.current[:dependencies] = []
  logger = Asciidoctor::MemoryLogger.new
  attributes = request.fetch('attributes', {}).merge({
    'showtitle' => '', 'attribute-missing' => 'warn', 'outfilesuffix' => '.xml',
    'allow-uri-read' => nil, 'max-include-depth' => 32,
    'localdate' => '1970-01-01', 'docdate' => '1970-01-01',
    'localyear' => '1970', 'docyear' => '1970',
    'localtime' => '00:00:00 UTC', 'doctime' => '00:00:00 UTC',
    'localdatetime' => '1970-01-01 00:00:00 UTC', 'docdatetime' => '1970-01-01 00:00:00 UTC',
    'docname' => File.basename(path, File.extname(path)), 'docfile' => file,
    'dita-topic-spaces' => 'off'
  })
  # Asciidoctor prepends these files using its own reader, preserving include semantics.
  prefix = request.fetch('attribute_files', []).map do |p|
    abs = File.expand_path(p, root)
    raise "Attribute file is outside input root: #{p}" unless abs.start_with?(root + File::SEPARATOR)
    raise "Missing attribute file: #{p}" unless File.file?(abs)
    "include::#{abs}[]"
  end.join("\n")
  source = request.key?('source') ? request.fetch('source') : File.read(file, encoding: 'UTF-8')
  input = (prefix.empty? ? '' : prefix + "\n\n") + source
  doc = Asciidoctor.load input, safe: :unsafe, base_dir: File.dirname(file), backend: 'dita-topic',
    attributes: attributes, logger: logger, sourcemap: true, header_footer: true,
    docfile: file
  raise 'Add a document title, for example: = Install the plugin' unless doc.doctitle
  # Never allow the upstream converter's SecureRandom fallback for pasted text.
  doc.id ||= 'topic-' + Digest::SHA256.hexdigest(path)[0, 16]
  # Avoid snapshot directory names in substitutions.
  doc.attributes['docfile'] = path
  doc.attributes['docdir'] = File.dirname(path)
  kind = doc.attr('_mod-docs-content-type') || doc.attr('_content-type') || doc.attr('_module-type')
  xml = doc.convert
  diagnostics = logger.messages.map do |m|
    value = m[:message]
    message = value.is_a?(Hash) ? value[:text].to_s : value.to_s
    # Cross-file targets need review; all potential content-loss warnings block output.
    severity = message.include?('Possible invalid reference:') ? 'warning' : 'error'
    location = value.is_a?(Hash) ? value[:source_location] : nil
    { severity: severity, message: message.gsub(root + '/', ''),
      line: location&.lineno }
  end
  { path: path, xml: xml, content_type: kind, diagnostics: diagnostics,
    dependencies: Thread.current[:dependencies].uniq.sort }
rescue StandardError => e
  { path: request['path'], xml: nil, diagnostics: [{ severity: 'error', message: e.message.gsub(request.fetch('root', '') + '/', '') }],
    dependencies: Thread.current[:dependencies] || [] }
end

requests = JSON.parse(STDIN.read)
STDOUT.write(JSON.generate(requests.map { |request| convert_one(request) }))
