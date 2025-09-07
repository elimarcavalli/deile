import os
import re
import json
from datetime import datetime
from pathspec import PathSpec
from pathspec.patterns import GitWildMatchPattern

ARQUIVO_PERSONA = "agent/persona.md"
PASTA_LOGS_API = "requests"

def carregar_persona():
    try:
        with open(ARQUIVO_PERSONA, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None

def ler_conteudo_arquivo(caminho):
    try:
        with open(caminho, 'r', encoding='utf-8') as arquivo:
            return arquivo.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"Arquivo '{caminho}' não encontrado.")
    except IOError as e:
        raise IOError(f"Erro ao ler o arquivo '{caminho}': {e}")

def sanitizar_resposta_ia(conteudo_ia):
    padrao = r"^\s*```(?:\w+)?\n(.*?)\n```\s*$"
    match = re.search(padrao, conteudo_ia, re.DOTALL)
    return match.group(1).strip() if match else conteudo_ia.strip()

def salvar_log_json(timestamp, tipo, payload):
    os.makedirs(PASTA_LOGS_API, exist_ok=True)
    nome_arquivo = os.path.join(PASTA_LOGS_API, f"{timestamp}_{tipo}.json")
    try:
        with open(nome_arquivo, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return nome_arquivo
    except (TypeError, IOError) as e:
        raise IOError(f"Erro ao salvar o log JSON em '{nome_arquivo}': {e}")

def sobrescrever_arquivo(caminho, conteudo):
    try:
        diretorio = os.path.dirname(caminho)
        if diretorio:
            os.makedirs(diretorio, exist_ok=True)
        conteudo_sanitizado = sanitizar_resposta_ia(conteudo)
        with open(caminho, 'w', encoding='utf-8') as arquivo:
            arquivo.write(conteudo_sanitizado)
    except IOError as e:
        raise IOError(f"Erro ao sobrescrever o arquivo '{caminho}': {e}")

def escanear_arquivos_do_projeto(diretorio_raiz='.'):
    gitignore_path = os.path.join(diretorio_raiz, '.gitignore')
    patterns = []
    if os.path.exists(gitignore_path):
        with open(gitignore_path, 'r') as f:
            patterns = [GitWildMatchPattern(p) for p in f.read().splitlines() if p and not p.startswith('#')]
    
    spec = PathSpec(patterns)
    arquivos_encontrados = []
    
    for root, dirs, files in os.walk(diretorio_raiz):
        dirs[:] = [d for d in dirs if not spec.match_file(os.path.join(root, d))]
        for file in files:
            caminho_completo = os.path.join(root, file)
            if not spec.match_file(caminho_completo):
                caminho_relativo = os.path.relpath(caminho_completo, diretorio_raiz).replace('\\', '/')
                arquivos_encontrados.append(caminho_relativo)
                
    return sorted(arquivos_encontrados)