FROM apache/airflow:3.3.0-python3.12

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV ARTSEARCH_PROJECT_ROOT=/opt/artsearch

COPY --chown=airflow:root . /opt/artsearch

USER airflow
WORKDIR /opt/artsearch
RUN pip install --no-cache-dir --editable ".[data,embed,filter,qdrant]" \
    "apache-airflow-providers-apache-spark==6.3.0" \
    "pyspark==4.2.0"
