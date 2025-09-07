# import os
# import re
# from collections import deque
# from datetime import datetime
# from . import ui_manager, file_manager, gemini_client
# from google.api_core.exceptions import ResourceExhausted

# HISTORICO_CONVERSA = deque(maxlen=20)

# def parse_ia_file_response(resposta_ia):
#     padrao = r'<file path="(.+?)">(.*?)</file>'
#     return {caminho: conteudo for caminho, conteudo in re.findall(padrao, resposta_ia, re.DOTALL)}

# def traduzir_prompt_para_backend(prompt_usuario):
#     return re.sub(r'@([^\s]+)', r'@@\1@@', prompt_usuario)

# def otimizar_historico_e_prompt(history_list, current_prompt):
#     """
#     [LÓGICA ROBUSTA] Otimiza a lista de histórico e o prompt atual para a API.
#     Garante que o conteúdo de cada arquivo apareça apenas na sua menção mais recente.
#     """
#     mensagens_combinadas = history_list + [{'role': 'user', 'parts': [{'text': current_prompt}]}]
    
#     padrao_conteudo = re.compile(r"@@(.+?)@@:\{(.*?)\}", re.DOTALL)
    
#     # Primeiro, encontra a última (mais recente) posição de cada arquivo com conteúdo
#     ultima_mencao_pos = {}
#     for i, msg in enumerate(mensagens_combinadas):
#         texto = msg['parts'][0]['text']
#         for match in padrao_conteudo.finditer(texto):
#             caminho = match.group(1)
#             ultima_mencao_pos[caminho] = i

#     # Agora, reconstrói a lista de mensagens otimizadas
#     historico_otimizado = []
#     prompt_otimizado = ""

#     for i, msg in enumerate(mensagens_combinadas):
#         texto_original = msg['parts'][0]['text']
#         texto_modificado = texto_original

#         # Se esta mensagem NÃO é a última menção de um arquivo, seu conteúdo deve ser removido
#         for match in padrao_conteudo.finditer(texto_original):
#             caminho = match.group(1)
#             if i < ultima_mencao_pos.get(caminho, -1):
#                 bloco_com_conteudo = match.group(0)
#                 marcador_sem_conteudo = f"@@{caminho}@@"
#                 texto_modificado = texto_modificado.replace(bloco_com_conteudo, marcador_sem_conteudo)
        
#         if i == len(mensagens_combinadas) - 1:
#             prompt_otimizado = texto_modificado
#         else:
#             historico_otimizado.append({'role': msg['role'], 'parts': [{'text': texto_modificado}]})

#     return historico_otimizado, prompt_otimizado

# def main():
#     os.system('cls' if os.name == 'nt' else 'clear')
#     ui = ui_manager.UIManager()
#     try:
#         persona = file_manager.carregar_persona()
#         if not persona:
#             ui.exibir_aviso(f"Arquivo '{file_manager.ARQUIVO_PERSONA}' não encontrado.")
#             persona = "Você é um assistente de IA prestativo."

#         client = gemini_client.GeminiClient(system_instruction=persona)
        
#         with ui.exibir_status("Escaneando arquivos do projeto..."):
#             file_list_cache = file_manager.escanear_arquivos_do_projeto()
#         ui.inicializar_session(file_list_cache)
#     except Exception as e:
#         ui.exibir_erro(f"Erro na inicialização: {e}")
#         return

#     ui.imprimir_cabecalho()
#     ui.exibir_sucesso(f"{len(file_list_cache)} arquivos indexados para autocompletar.")

#     while True:
#         try:
#             file_list_cache = file_manager.escanear_arquivos_do_projeto()
#             ui.atualizar_lista_arquivos(file_list_cache)
#             prompt_usuario = ui.obter_prompt_usuario()
            
#             if not prompt_usuario.strip(): continue
#             if prompt_usuario.lower() in ["sair", "exit", "quit"]:
#                 ui.console.print("[bold yellow]DEILE se despedindo. Até a próxima! 👋[/bold yellow]")
#                 break

#             prompt_backend = traduzir_prompt_para_backend(prompt_usuario)
#             prompt_para_ia = prompt_backend

#             caminhos_ja_injetados = set()
#             def injetar_conteudo_unico(match):
#                 caminho = match.group(1)
#                 if caminho in caminhos_ja_injetados: return f"@@{caminho}@@"
#                 try:
#                     conteudo = file_manager.ler_conteudo_arquivo(caminho)
#                     caminhos_ja_injetados.add(caminho)
#                     # [CORREÇÃO CRÍTICA] Garante que o formato "@@caminho@@:{conteudo}" seja sempre criado corretamente.
#                     return f"@@{caminho}@@:{{{conteudo}}}"
#                 except (FileNotFoundError, IOError) as e:
#                     ui.exibir_erro(str(e)); caminhos_ja_injetados.add(caminho)
#                     return f"@@{caminho}@@:{{ERRO: ARQUIVO NÃO ENCONTRADO}}"
            
#             prompt_para_ia = re.sub(r'@@(.+?)@@', injetar_conteudo_unico, prompt_para_ia)
            
#             history_list_api = list(HISTORICO_CONVERSA)
            
#             # --- OTIMIZAÇÃO DO HISTÓRICO E PROMPT ATUAL ---
#             historico_otimizado, prompt_otimizado = otimizar_historico_e_prompt(history_list_api, prompt_para_ia)
            
#             with ui.exibir_status("DEILE está processando... 🧠"):
#                 resposta_ia_crua, request_payload, response_payload = client.gerar_conteudo(
#                     history=historico_otimizado, 
#                     user_prompt=prompt_otimizado
#                 )
                
#                 timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
#                 file_manager.salvar_log_json(timestamp, "request", request_payload)
#                 file_manager.salvar_log_json(timestamp, "response", response_payload)

#             # Adiciona a interação atual (NÃO otimizada) ao histórico para manter o registro fiel
#             HISTORICO_CONVERSA.append({'role': 'user', 'parts': [{'text': prompt_para_ia}]})
#             HISTORICO_CONVERSA.append({'role': 'model', 'parts': [{'text': resposta_ia_crua}]})

#             arquivos_para_alterar = parse_ia_file_response(resposta_ia_crua)

#             if arquivos_para_alterar:
#                 ui.exibir_sucesso(f"IA propôs alterações para {len(arquivos_para_alterar)} arquivo(s).")
#                 for caminho, novo_conteudo in arquivos_para_alterar.items():
#                     if ui.confirm_action(f"Deseja sobrescrever o arquivo '{caminho}'?"):
#                         try:
#                             file_manager.sobrescrever_arquivo(caminho, novo_conteudo)
#                             ui.exibir_sucesso(f"Arquivo '{caminho}' foi atualizado com sucesso!")
#                         except IOError as e: ui.exibir_erro(str(e))
#                     else: ui.exibir_aviso(f"Alteração no arquivo '{caminho}' foi cancelada.")
#             else:
#                 ui.exibir_resposta_simples(resposta_ia_crua)

#         except ResourceExhausted:
#             ui.exibir_erro("Limite de requisições da API atingido. Aguarde um minuto.")
#         except KeyboardInterrupt:
#             ui.console.print("\n[bold yellow]DEILE se despedindo. Até a próxima! 👋[/bold yellow]")
#             break
#         except Exception as e:
#             ui.exibir_erro(f"Ocorreu um erro inesperado: {e}")