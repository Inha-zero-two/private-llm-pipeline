"""원본 Llama.__call__ 방식으로 텍스트를 완성한다."""
import json
import sys

from llama_wrapper import Llama

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

with Llama(model_path="models/gemma.gguf", n_ctx=65536) as llm:
    response = llm("The capital of South Korea is", max_tokens=32, temperature=0, stop=["\n"])
    print(json.dumps(response, ensure_ascii=False, indent=2))
    print("생성된 텍스트:", response["choices"][0]["text"])
