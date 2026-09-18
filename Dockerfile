# AsciiDoc -> DITA browser tool, containerized for OpenShift.
#
# Runtime needs Python (converter CLI/HTTP server, lxml, dita-convert) AND Ruby
# (asciidoctor + asciidoctor-dita-topic, invoked as a subprocess via `bundle exec`).
# UBI9 provides both as AppStream modules, so a single-stage build keeps this simple.
FROM registry.access.redhat.com/ubi9/ubi:latest

# python3.12 + ruby 3.3 (matches ci/github-actions.yml), git (used by the compare
# command), and headers/compiler in case a pinned Python wheel (lxml) has no
# prebuilt manylinux wheel for this platform and must build from source.
# rubygem-json is required explicitly: scripts/convert.rb does `require 'json'`,
# but this minimal Ruby module install does not ship the json default gem's
# compiled extension, unlike full-featured Ruby installs (rbenv/rvm/Homebrew).
RUN dnf module enable -y ruby:3.3 && \
    dnf install -y --setopt=install_weak_deps=False \
        python3.12 python3.12-pip python3.12-devel \
        ruby ruby-devel rubygems rubygem-json \
        git gcc libxml2-devel libxslt-devel \
        shadow-utils && \
    dnf clean all && rm -rf /var/cache/dnf

WORKDIR /opt/app-root/src

# Python dependencies in an isolated venv.
COPY requirements.txt ./
RUN python3.12 -m venv /opt/app-root/venv && \
    /opt/app-root/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/app-root/venv/bin/pip install --no-cache-dir -r requirements.txt

# Ruby dependencies, installed into the project (mirrors scripts/setup.sh).
ENV BUNDLE_GEMFILE=/opt/app-root/src/Gemfile \
    BUNDLE_PATH=/opt/app-root/src/vendor/bundle \
    BUNDLE_USER_CACHE=/opt/app-root/src/.cache/bundle \
    XDG_CACHE_HOME=/opt/app-root/src/.cache
COPY Gemfile Gemfile.lock ./
RUN gem install bundler --no-document && bundle install

# Application source.
COPY . .

# Support OpenShift's arbitrary, non-root UID (random UID, group 0).
RUN chgrp -R 0 /opt/app-root/src && chmod -R g=u /opt/app-root/src

ENV PATH="/opt/app-root/venv/bin:${PATH}" \
    PYTHONPATH=/opt/app-root/src \
    PYTHONUNBUFFERED=1 \
    HOME=/opt/app-root/src \
    ADOC_DITA_BIND_HOST=127.0.0.1 \
    ADOC_DITA_ALLOWED_HOSTS=""

EXPOSE 8765
USER 1001

ENTRYPOINT ["/opt/app-root/venv/bin/python", "-m", "adoc_dita.cli", "serve", "--port", "8765"]
