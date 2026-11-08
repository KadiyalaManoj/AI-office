# Throwaway image for `--sandbox docker`: Python, Node and Terraform, with no project code and no secrets.
# Built automatically the first time with: docker build -t ai-office-checks agents/sandbox
FROM hashicorp/terraform:1.9 AS terraform

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest ruff mypy
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
WORKDIR /work
