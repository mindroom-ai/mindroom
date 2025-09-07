FROM public.ecr.aws/docker/library/python:3.11-slim

# Install kubectl and helm
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

# Install Helm
RUN curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

WORKDIR /app

# Copy requirements and install
COPY services/instance-provisioner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY services/instance-provisioner/app.py .

# Copy Helm charts
COPY k8s/instance /app/k8s/instance

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8002

CMD ["python", "app.py"]
