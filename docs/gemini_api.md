# Documentação: Gemini API (Atualizada)

**Versão do documento:** 2025-09-05

> Objetivo: arquivo-base de conhecimento para agentes sobre o uso correto e atualizado da Gemini API (Google Gen AI). Contém recomendações, exemplos práticos em Python com o SDK novo (`google-genai`), notas de migração e dependências.

---

## 1. Visão geral — resumo rápido

* **SDK recomendado (novo):** `google-genai` (Python). Use `from google import genai` e `genai.Client()`.
* **SDK legacy:** `google-generativeai` (antigo/deprecated). Funcionalidade limitada; migrar para o novo SDK.
* **Modelos suportados (exemplos):** Gemini 2.5 Pro, Gemini 2.5 Flash, Gemini 2.5 Flash-Lite, Veo 3, Gemini 2.5 Flash Image, Gemini Embeddings. Variantes podem variar por projeto/conta.
* **Uso principal:** geração de texto multimodal, function-calling (integração com ferramentas/APIs externas), imagens (Gemini 2.5 Flash Image), long context, structured output, thinking.

---

## 2. Dependências recomendadas

* Python **3.9+**.
* `google-genai` — instalar via `pip install google-genai` (usar a versão mais recente compatível; exemplo público: 0.6.0 em 2025).
* **(Legacy only)** `google-generativeai` — só se precisar de compatibilidade; planeje migrar.

> Observação: sempre verifique compatibilidade de versões no PyPI/documentação oficial antes do deploy e execute testes em sua conta/projeto (cotas e disponibilidade de modelo variam por projeto).

---

## 3. Principais conceitos

* **Client centralizado (novo SDK):** `genai.Client(api_key=...)` é a entrada do SDK para operações.
* **GenerateContent/Models:** o fluxo principal para gerar conteúdo é `client.models.generate_content(...)` ou APIs equivalentes no SDK.
* **Function calling / Tools:** declare funções (`FunctionDeclaration`) com `parameters` em formato de `Schema` (usar tipos enumerados: `OBJECT`, `STRING`, `ARRAY`, `NUMBER`, `BOOLEAN`, `INTEGER`) para evitar erros de schema.

---

## 4. Formato recomendado de `FunctionDeclaration` (resumo prático)

* Campos principais:

  * `name` (string): identificador da função.
  * `description` (string): texto explicativo.
  * `parameters` (Schema-like): objeto com `type` (use `OBJECT`), `properties` (mapa de nomes para tipos) e `required` (lista).

* **Tipos** devem ser declarados em maiúsculas quando o SDK espera enums (ex.: `"type": "OBJECT"`, `"properties": { "location": { "type": "STRING" } }`).

* **Fluxo:** modelo retorna indicação de chamada de função + parâmetros → sua aplicação executa a função externa → passe o resultado de volta ao modelo se desejar continuação do diálogo.

---

## 5. Exemplo prático (Novo SDK `google-genai`) — Python

```python
# pip install google-genai
from google import genai
from google.genai.types import FunctionDeclaration, GenerateContentConfig, Tool

# Definição de função
get_weather = FunctionDeclaration(
    name="get_weather",
    description="Retorna o tempo atual para uma localidade",
    parameters={
        "type": "OBJECT",
        "properties": {
            "location": {"type": "STRING", "description": "cidade, estado/país"},
            "units": {"type": "STRING", "description": "metric or imperial"}
        },
        "required": ["location"]
    }
)

weather_tool = Tool(function_declarations=[get_weather])

client = genai.Client(api_key="SUA_API_KEY")

config = GenerateContentConfig(tools=[weather_tool])

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="What's the weather in Boston?",
    config=config
)

print(response)
```

> Observação prática: verifique se o objeto `FunctionDeclaration`/`Tool` está sendo instanciado conforme a API do SDK; alguns erros surgem ao tentar enviar um dict cru quando o SDK espera tipos proto/objetos tipados.

---

## 6. Exemplo (Legacy `google-generativeai`) — Apenas referência

```python
# pip install google-generativeai
import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration

genai.configure(api_key="SUA_API_KEY")

fn = FunctionDeclaration(
    name="write_file",
    description="Cria/modifica arquivo",
    parameters={
        "type": "OBJECT",
        "properties": {
            "file_path": {"type": "STRING"},
            "content": {"type": "STRING"}
        },
        "required": ["file_path", "content"]
    }
)

model = genai.GenerativeModel(model_name="gemini-1.5-pro-latest", tools=[fn])
response = model.generate_content("Create a hello.py file")
```

> Planeje migrar esse código para o novo SDK: `google-genai`.

---

## 7. Erros comuns e correções rápidas

* **Protocol message Schema has no 'type' field**: normalmente causado por `parameters` num formato incorreto. Use `FunctionDeclaration`/Schema com tipos enumerados (OBJECT/STRING/etc.).
* **KeyError / Struct field missing**: checar `properties` e `required`.
* **Tool não executada**: confirmar que as `FunctionDeclaration` foram registradas corretamente em `tools`/`config` e que o modelo tem permissão/ability para chamar funções.

---

## 8. Migração (legacy → novo) — passos práticos

1. Instale `google-genai` e atualize imports: `from google import genai`.
2. Substitua instâncias/usage de `GenerativeModel(...)` por `client.models.generate_content(...)` e converta `FunctionDeclaration`/`Tool` para os tipos do novo SDK.
3. Teste cada ferramenta: primeiro confirme que o modelo retorna a intenção de chamada (sem executar), depois execute a função e repasse o resultado para o modelo se desejar iteração adicional.

---

## 9. Modelos, disponibilidade e limites

* Nomes de modelos (ex.: `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`) podem variar e sua conta/projeto pode não ter acesso a todos. Confirme na Console/API antes do deploy.
* Algumas variantes (Flash / Pro / Lite) otimizam trade-offs entre custo, latência e capacidade de raciocínio/razonamento.

---

## 10. Changelog e notas de depreciação importantes

* SDK `google-generativeai` declarado legacy — suporte e correções limitadas; planejar migração.
* Recomenda-se usar `google-genai` para novos projetos; acompanhe PyPI e guia oficial.

---

## 11. Recursos úteis (para referência do engenheiro)

* Documentação oficial Gemini API / Function calling (Google AI dev docs)
* PyPI: `google-genai`, `google-generativeai`
* Guia de Function Calling e exemplos no Vertex AI


---

*Fim do documento.*
