# alrust alpha server image.
#   docker build -t alrust:alpha .
# Build stage: the pinned toolchain from rust-toolchain.toml.
FROM docker.io/library/rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked --bin alrust

# Runtime stage: a small Debian base with CA certificates only.
FROM docker.io/library/debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home alrust
COPY --from=build /src/target/release/alrust /usr/local/bin/alrust
USER alrust
EXPOSE 8080
ENV ALRUST_BIND=0.0.0.0:8080
ENTRYPOINT ["/usr/local/bin/alrust"]
