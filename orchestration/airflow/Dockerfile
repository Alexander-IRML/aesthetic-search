ARG AIRFLOW_VERSION=3.3.1
FROM apache/airflow:${AIRFLOW_VERSION}-python3.12

ARG AIRFLOW_VERSION

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libgomp1 \
        openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV ARTSEARCH_PROJECT_ROOT=/opt/artsearch
ENV PYSPARK_DRIVER_PYTHON=/usr/local/bin/python
ENV PYSPARK_PYTHON=/usr/local/bin/python

USER airflow
WORKDIR /opt/artsearch
COPY --chown=airflow:root .python-version /tmp/artsearch-python-version
RUN test "$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" = \
        "$(cut -d. -f1-2 /tmp/artsearch-python-version)"
COPY --chown=airflow:root orchestration/airflow/requirements.txt /tmp/airflow-requirements.txt
RUN pip install --no-cache-dir \
    --requirement /tmp/airflow-requirements.txt \
    --constraint "${HOME}/constraints.txt"
COPY --chown=airflow:root constraints/runtime-py312.txt /tmp/runtime-py312.txt
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    --constraint /tmp/runtime-py312.txt \
    "torch==2.13.0" \
    "torchvision==0.28.0"

USER root
COPY --chown=airflow:root . /opt/artsearch

USER airflow
# PySpark 4.2 warns that pandas 3.x is not fully supported. Airflow's upstream
# constraints currently select pandas 3.0, so the final compatible environment
# deliberately returns to the latest pandas 2.x release.
RUN pip install --no-cache-dir \
    --constraint /tmp/runtime-py312.txt \
    "apache-airflow==${AIRFLOW_VERSION}" \
    "pandas==2.3.3" \
    ".[data,embed,filter,postgres,qdrant]" \
    && pip check
