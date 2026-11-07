# alrust alpha server image.
#   docker build -t alrust:alpha .
# Build stage: the pinned toolchain from rust-toolchain.toml.
FROM docker.io/library/rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e AS build
WORKDIR /src
COPY . .
# The notice corpus is tied to this reviewed lock/selection, not inferred from
# whichever dependencies happen to be cached in a builder.
RUN test "$(rustc -vV | sed -n 's/^host: //p')" = x86_64-unknown-linux-gnu \
    && sha256sum --check third_party/rust/SHA256SUMS \
    && cargo build --release --locked --package alrust-server --bin alrust --target x86_64-unknown-linux-gnu
# Retain the actual builder standard-library notices with the distributed binary.
# Missing notices fail the build; this does not claim full image/license clearance.
RUN set -eu; \
    sysroot="$(rustc --print sysroot)"; \
    mkdir -p /out/rust-standard-library; \
    test -s "$sysroot/share/doc/rust/COPYRIGHT-library.html"; \
    test -s "$sysroot/share/doc/rust/licenses/MIT.txt"; \
    test -s "$sysroot/share/doc/rust/licenses/Apache-2.0.txt"; \
    cp "$sysroot/share/doc/rust/COPYRIGHT-library.html" /out/rust-standard-library/; \
    cp -R "$sysroot/share/doc/rust/licenses" /out/rust-standard-library/

# Runtime stage: a small Debian base with CA certificates only.
FROM docker.io/library/debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home alrust
COPY --from=build /src/target/x86_64-unknown-linux-gnu/release/alrust /usr/local/bin/alrust
COPY third_party/alerta /usr/share/licenses/alrust/alerta/
COPY THIRD_PARTY_NOTICES.md /usr/share/licenses/alrust/THIRD_PARTY_NOTICES.md
COPY --from=build /src/third_party/rust /usr/share/licenses/alrust/rust/
COPY --from=build /out/rust-standard-library /usr/share/licenses/alrust/rust-standard-library/
USER alrust
EXPOSE 8080
ENV ALRUST_BIND=0.0.0.0:8080
ENTRYPOINT ["/usr/local/bin/alrust"]
