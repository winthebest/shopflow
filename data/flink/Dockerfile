# syntax=docker/dockerfile:1
# Build context: data/flink. Flink 2.2 + the connectors the KPI job needs (Kafka, JDBC + Postgres driver; jars pinned
# by sha256, cross-checked against Maven Central's sha1 when pinned), the S3 filesystem plugin for checkpoints,
# a small SQL runner (sql-runner/, compiled against Flink's own jars) and the SQL it runs. The SQL is baked in, like
# the Airflow DAGs: the job, its connectors and its SQL ship as one tested artifact (docs/adr/0415).
ARG FLINK_IMAGE=flink:2.2.1-java17@sha256:140c3909f06cbea741be78fc630e809fae9dadcbfa492a684acf39efbbf68946

FROM ${FLINK_IMAGE} AS flink

FROM eclipse-temurin:17-jdk@sha256:5d6042fb8cdc14d614e4e421f52e3211fc5aeadddce44cbf9de8ed37c791f824 AS runner
COPY --from=flink /opt/flink/lib /opt/flink/lib
WORKDIR /src
COPY sql-runner/src ./src
RUN javac --release 17 -cp '/opt/flink/lib/*' -d classes $(find src -name '*.java') \
    && jar --create --file sql-runner.jar -C classes .

FROM ${FLINK_IMAGE}
ARG MAVEN=https://repo1.maven.org/maven2
LABEL org.opencontainers.image.source="https://github.com/winthebest/shopflow" \
      org.opencontainers.image.title="shopflow-flink" \
      org.opencontainers.image.description="shopflow Flink 2.2: Kafka/JDBC connectors, S3 checkpoints, KPI SQL job"
ADD --checksum=sha256:5605c691d11a501382c383fecba37a7a552467da5ab7ba904ef5d6f3d62c5616 --chmod=644 \
    ${MAVEN}/org/apache/flink/flink-sql-connector-kafka/5.0.0-2.2/flink-sql-connector-kafka-5.0.0-2.2.jar /opt/flink/lib/
# JDBC 4.0.0-2.0, not 4.1.0-2.2: 4.1 registers an OpenLineage extractor (ServiceLoader) whose client library and its
# Jackson/native dependencies are not bundled. The Flink 2.x connector API is the same on 2.2.
ADD --checksum=sha256:ccb9691bf1d0f97574ae768e1bef05a39c06d7f7b13996e9d485fbdd69dcbfec --chmod=644 \
    ${MAVEN}/org/apache/flink/flink-connector-jdbc-core/4.0.0-2.0/flink-connector-jdbc-core-4.0.0-2.0.jar /opt/flink/lib/
ADD --checksum=sha256:b59c29dc65cafe8ae398e69072b9f6022f4a978a09002fd06a05a1a8dd3348d6 --chmod=644 \
    ${MAVEN}/org/apache/flink/flink-connector-jdbc-postgres/4.0.0-2.0/flink-connector-jdbc-postgres-4.0.0-2.0.jar /opt/flink/lib/
ADD --checksum=sha256:73914527305a40cce504b0d3d90b23caf565136912d607ae8ae7c5895512332c --chmod=644 \
    ${MAVEN}/org/postgresql/postgresql/42.7.14/postgresql-42.7.14.jar /opt/flink/lib/
# Checkpoints go to SeaweedFS (S3 API) through the Hadoop S3A plugin shipped in opt/: its default credential chain
# reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from the pod (a Secret), so no key is written into the Flink
# configuration (a ConfigMap). The job's directories are made here (755): COPY --chmod would give the directories it
# creates the file mode too, and the flink user could not enter them.
RUN mkdir -p /opt/flink/plugins/s3-fs-hadoop /opt/flink/usrlib /opt/flink/sql \
    && cp /opt/flink/opt/flink-s3-fs-hadoop-*.jar /opt/flink/plugins/s3-fs-hadoop/
COPY --from=runner --chmod=644 /src/sql-runner.jar /opt/flink/usrlib/sql-runner.jar
COPY --chmod=644 kpi_minute.sql /opt/flink/sql/kpi_minute.sql
