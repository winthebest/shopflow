# Kafka Connect runtime for shopflow: Strimzi Kafka image + Debezium Postgres source + Iceberg sink.
# Built and pushed by .github/workflows/connect-image.yml and used via KafkaConnect.spec.image (no spec.build,
# so the cluster never needs registry credentials). Versions are chosen in docs/adr/0400-*.md.
#
# Every downloaded artifact is checked against a sha512 pinned in this file, so a changed upstream file fails
# the build instead of silently entering the image.

ARG STRIMZI_KAFKA_IMAGE=quay.io/strimzi/kafka:1.2.0-kafka-4.3.1@sha256:fef34b5438e8556cc08c01f3e254e47346f061b53a4e38d4289853777e0ea7f1
ARG JDK_IMAGE=eclipse-temurin:21-jdk@sha256:3e3c176ffed168beb42c607be9bc1639b466cf00261a0fb04425562c9d0c5c2b

# Apache publishes no binary for the Iceberg Kafka Connect runtime, so it is built from the signed source release.
# The output is plain jars, so this stage runs on the build host's architecture (no QEMU) for every target.
FROM --platform=$BUILDPLATFORM ${JDK_IMAGE} AS iceberg-runtime
ARG ICEBERG_VERSION=1.12.0
ARG ICEBERG_SOURCE_SHA512=505b4d92baf9a2b55c3893bfa1b50289a0bf0051109d81f0a92825f406c28be9096be31d5c22ed68a2d7abd39fcfb0888729bb1917216b06ac200ac0fbf11a28
WORKDIR /src
# The Apache CDN only serves current releases; older ones move to the (slow) archive, kept as the fallback.
RUN release="iceberg/apache-iceberg-${ICEBERG_VERSION}/apache-iceberg-${ICEBERG_VERSION}.tar.gz" \
 && { curl -fsSL --retry 3 --max-time 300 -o iceberg.tar.gz "https://dlcdn.apache.org/${release}" \
      || curl -fsSL --retry 3 --max-time 900 -o iceberg.tar.gz "https://archive.apache.org/dist/${release}"; } \
 && echo "${ICEBERG_SOURCE_SHA512}  iceberg.tar.gz" | sha512sum -c - \
 && tar -xzf iceberg.tar.gz --strip-components=1 \
 && rm iceberg.tar.gz
# Only the Kafka Connect modules are configured (no Spark/Flink), tests are skipped: they run upstream per release.
RUN --mount=type=cache,target=/root/.gradle \
    ./gradlew --no-daemon --console=plain -DsparkVersions= -DflinkVersions= -DkafkaVersions=3 \
      -x test -x integrationTest :iceberg-kafka-connect:iceberg-kafka-connect-runtime:distZip \
 && mkdir /out \
 && cd /out \
 && jar xf "/src/kafka-connect/kafka-connect-runtime/build/distributions/iceberg-kafka-connect-runtime-${ICEBERG_VERSION}.zip" \
 && mv "iceberg-kafka-connect-runtime-${ICEBERG_VERSION}" iceberg-kafka-connect

FROM --platform=$BUILDPLATFORM ${JDK_IMAGE} AS debezium-postgres
ARG DEBEZIUM_VERSION=3.7.0.Final
ARG DEBEZIUM_POSTGRES_SHA512=dc5334e087c7405f5b423e77373c60a89a3d7538ad38016f3959c431ad7078361ab95300a7e926c50621e7aa8375b7f4f3c5ecec4fe04ac484804d325009b6c0
WORKDIR /out
RUN curl -fsSL --retry 3 -o debezium.tar.gz \
      "https://repo1.maven.org/maven2/io/debezium/debezium-connector-postgres/${DEBEZIUM_VERSION}/debezium-connector-postgres-${DEBEZIUM_VERSION}-plugin.tar.gz" \
 && echo "${DEBEZIUM_POSTGRES_SHA512}  debezium.tar.gz" | sha512sum -c - \
 && tar -xzf debezium.tar.gz \
 && rm debezium.tar.gz

FROM ${STRIMZI_KAFKA_IMAGE}
ARG ICEBERG_VERSION=1.12.0
ARG DEBEZIUM_VERSION=3.7.0.Final
LABEL org.opencontainers.image.source="https://github.com/winthebest/shopflow" \
      org.opencontainers.image.description="Strimzi Kafka Connect with Debezium Postgres ${DEBEZIUM_VERSION} and Apache Iceberg sink ${ICEBERG_VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0"
# Root-owned and read-only for the runtime user (1001), like the rest of /opt/kafka.
COPY --from=debezium-postgres /out/debezium-connector-postgres /opt/kafka/plugins/debezium-connector-postgres
COPY --from=iceberg-runtime /out/iceberg-kafka-connect /opt/kafka/plugins/iceberg-kafka-connect
USER 1001
