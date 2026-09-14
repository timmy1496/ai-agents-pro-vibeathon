# Образ ембеддера з запеченим GGUF: старт пода не залежить від Hugging Face.
# docker build -t kb-embedder:v2-moe-q8 -f infra/k8s/embedding/Dockerfile .
FROM ghcr.io/ggml-org/llama.cpp:server
ADD --chmod=644 https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe-GGUF/resolve/main/nomic-embed-text-v2-moe.Q8_0.gguf /models/nomic-embed-text-v2-moe.Q8_0.gguf
ENTRYPOINT ["/app/llama-server", "-m", "/models/nomic-embed-text-v2-moe.Q8_0.gguf", \
            "--embedding", "--pooling", "mean", "--ctx-size", "512", "--ubatch-size", "512", \
            "--batch-size", "512", "--host", "0.0.0.0", "--port", "8081"]
