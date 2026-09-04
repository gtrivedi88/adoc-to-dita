# frozen_string_literal: true
# Inspect the native parser's state at real include boundaries. No repository code is run.
require_relative 'convert'

module CaptureRepositoryContext
  def resolve_include_path(*args)
    result = super
    if result[1] == :file && File.file?(result[0])
      file = File.realpath(result[0])
      state = Thread.current[:repository_context]
      if state && state[:topics].key?(file)
        attrs = @document.attributes.select { |key, _| state[:names].include?(key) }
        parent = File.realpath(cursor.file)
        state[:occurrences] << { path: state[:topics][file]['path'], attributes: attrs.dup,
                                 parent: parent.delete_prefix(state[:root] + '/'),
                                 line: cursor.lineno - (parent == state[:guide_file] ? state[:prefix_lines] : 0) }
      end
    end
    result
  end
end
Asciidoctor::PreprocessorReader.prepend CaptureRepositoryContext

def inspect_guide(request)
  root = File.realpath(request.fetch('root'))
  file = File.realpath(File.join(root, request.fetch('guide')))
  raise 'Guide must stay within the repository' unless file.start_with?(root + '/')
  Thread.current[:conversion_root] = root
  Thread.current[:dependencies] = []
  topics = request.fetch('topics').to_h { |topic| [File.join(root, topic['path']), topic] }
  state = { root: root, guide_file: file, prefix_lines: request['attribute_file'] ? 2 : 0,
            topics: topics, names: request.fetch('attribute_names'), occurrences: [] }
  Thread.current[:repository_context] = state
  logger = Asciidoctor::MemoryLogger.new
  attrs = { 'allow-uri-read' => nil, 'attribute-missing' => 'warn', 'max-include-depth' => 64,
            'localdate' => '1970-01-01', 'docdate' => '1970-01-01',
            'localyear' => '1970', 'docyear' => '1970',
            'localtime' => '00:00:00 UTC', 'doctime' => '00:00:00 UTC',
            'localdatetime' => '1970-01-01 00:00:00 UTC', 'docdatetime' => '1970-01-01 00:00:00 UTC' }
  # Explicit user overrides also control conditional includes in the guide.
  attrs.update(request.fetch('attributes', {}))
  input = File.read(file, encoding: 'UTF-8')
  if request['attribute_file']
    attribute_file = File.realpath(File.join(root, request['attribute_file']))
    raise 'Attributes file must stay within the repository' unless attribute_file.start_with?(root + '/')
    input = "include::#{attribute_file}[]\n\n" + input
  end
  doc = Asciidoctor.load input, safe: :unsafe, base_dir: File.dirname(file), docfile: file,
                        sourcemap: true, logger: logger, attributes: attrs.merge('docfile' => file)
  references = doc.find_by(context: :section).filter_map do |node|
    location = node.source_location
    next unless location && location.file && File.file?(location.file)
    topic = topics[File.realpath(location.file)]
    next unless topic && location.lineno <= topic['title_line'] && node.id
    { path: topic['path'], id: node.id }
  end
  by_path = references.group_by { |ref| ref[:path] }
  occurrences = state[:occurrences].filter_map do |occurrence|
    ref = by_path[occurrence[:path]]&.shift
    next unless ref
    occurrence.merge(id: ref[:id])
  end
  { guide: request['guide'], title: doc.doctitle, occurrences: occurrences,
    references: references, dependencies: Thread.current[:dependencies].uniq.sort,
    diagnostics: logger.messages.map { |message| value = message[:message];
      { severity: message[:severity].to_s.downcase, message: (value.is_a?(Hash) ? value[:text] : value).to_s.gsub(root + '/', '') } } }
rescue StandardError => error
  { guide: request['guide'], title: request['guide'], occurrences: [], references: [], dependencies: [],
    diagnostics: [{ severity: 'error', message: error.message }] }
ensure
  Thread.current[:repository_context] = nil
end

STDOUT.write(JSON.generate(JSON.parse(STDIN.read).map { |request| inspect_guide(request) }))
