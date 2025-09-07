# """
# DEPRECATED: Este arquivo usa o SDK legacy google-generativeai

# ⚠️ AVISO DE DEPRECIAÇÃO:
# Este arquivo foi migrado para o novo SDK google-genai.
# Use deile.core.models.gemini_provider.GeminiProvider em vez desta classe.

# Este arquivo será removido em uma versão futura.
# """

# import os
# import warnings
# # Legacy SDK - migrado para google-genai no GeminiProvider
# import google.generativeai as genai
# from dotenv import load_dotenv
# from google.api_core.exceptions import ResourceExhausted

# warnings.warn(
#     "GeminiClient is deprecated. Use deile.core.models.gemini_provider.GeminiProvider instead.",
#     DeprecationWarning,
#     stacklevel=2
# )

# class GeminiClient:
#     def __init__(self, system_instruction, model_name='gemini-1.5-pro-latest'):
#         load_dotenv()
#         api_key = os.getenv("GOOGLE_API_KEY")
#         if not api_key:
#             raise ValueError("API Key não encontrada. Verifique seu arquivo .env")

#         genai.configure(api_key=api_key)
        
#         self.system_instruction = system_instruction

#         # A instrução do sistema (persona) agora é parte da configuração do modelo
#         self.model = genai.GenerativeModel(
#             model_name,
#             system_instruction=system_instruction
#         )
        
#         # Prepara a estrutura para o futuro uso de tools
#         self.tools = [] # Atualmente vazio, mas pronto para ser populado

#     def gerar_conteudo(self, history, user_prompt):
#         try:
#             # O histórico agora é gerenciado pelo próprio objeto de chat
#             chat = self.model.start_chat(history=history)
            
#             # O payload do request é implicitamente construído pela biblioteca
#             # Nós salvamos uma representação dele para o log
#             request_payload = {
#                 'model': self.model.model_name,
#                 'system_instruction': self.system_instruction,
#                 'tools': self.tools,
#                 'contents': history + [{'role': 'user', 'parts': [{'text': user_prompt}]}]
#             }
            
#             # Envia apenas a nova mensagem do usuário
#             response = chat.send_message(user_prompt)
            
#             # Constrói o payload de resposta para o log
#             response_payload = {
#                 "candidates": [
#                     {
#                         "content": {
#                             "parts": [{"text": part.text for part in candidate.content.parts}],
#                             "role": candidate.content.role
#                         },
#                         "finish_reason": candidate.finish_reason.name,
#                         "index": i,
#                     }
#                     for i, candidate in enumerate(response.candidates)
#                 ],
#                 "usage_metadata": {
#                     "prompt_token_count": response.usage_metadata.prompt_token_count,
#                     "candidates_token_count": response.usage_metadata.candidates_token_count,
#                     "total_token_count": response.usage_metadata.total_token_count
#                 }
#             }
            
#             return response.text, request_payload, response_payload
#         except ResourceExhausted:
#             raise
#         except Exception as e:
#             raise RuntimeError(f"Erro ao chamar a API do Gemini: {e}")