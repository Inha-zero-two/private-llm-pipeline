"""생성되는 답변을 스트리밍으로 출력한다."""

import sys
from contextlib import closing

from llama_wrapper import Llama

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

with closing(Llama(model_path="models/gemma.gguf", n_ctx=65536)) as llm:
    # 로컬 원본 라이브러리에는 서버 전용 템플릿 옵션을 전달하지 않는다.
    options = (
        {"chat_template_kwargs": {"enable_thinking": False}}
        if getattr(llm, "backend", "local") == "remote"
        else {}
    )
    for response in llm.create_chat_completion(
        messages=[{"role": "user", "content": "파이썬의 장점 세 가지를 짧게 알려줘."}],
        max_tokens=512,
        temperature=0.2,
        stream=True,
        **options,
    ):
        choices = response.get("choices", [])
        if choices:
            print(choices[0].get("delta", {}).get("content") or "", end="", flush=True)
    print()
