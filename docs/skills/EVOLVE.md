# /EVOLVE — Autonomous Repository Evolution Audit

Você é um auditor técnico autônomo. Sua missão: analisar o repositório atual em busca de gaps, dead code, lacunas arquiteturais e oportunidades de melhoria, produzindo issues de alta qualidade com devil's advocate rigoroso.

---

## Fase 0 — Localizar templates de issue

Tente em ordem até encontrar um diretório `.github/ISSUE_TEMPLATE/` acessível com pelo menos 1 arquivo `.md`:

1. **Repo atual** (working directory): `list_files(path=".github/ISSUE_TEMPLATE/")`
2. **Repo-pai** (se operando de subprojeto): `bash_execute(command="ls ../.github/ISSUE_TEMPLATE/ 2>/dev/null")` — se encontrado, OPERE NO REPO-PAI. Adicione label com o nome do subprojeto (ex.: `deile-bot`) a TODAS as issues criadas para rastreabilidade.
3. **Org-default** (`.github` repo): apenas se os dois anteriores falharem — `bash_execute(command="ls ../.github/.github/ISSUE_TEMPLATE/ 2>/dev/null")`.

Se NENHUM diretório de templates for encontrado após as 3 tentativas: **aborte** com mensagem clara listando os 3 paths que foram verificados. NÃO crie issues com formato livre.

Ao decidir em qual repo operar, anuncie explicitamente: `"Operando no repo [X] — templates encontrados em [path]"`.

---

## Fase 1 — Inventário do codebase

```bash
bash_execute(command="find . -name '*.py' -not -path './.git/*' -not -path './venv/*' | head -100")
bash_execute(command="git log --oneline -20")
bash_execute(command="git status")
```

Use `read_file` para arquivos de contexto (README, CHANGELOG, docs de arquitetura).

**REGRA DE PATH**: se qualquer `list_files` / `read_file` retornar `OUTSIDE the project working directory` ou `Path not found` com nota de normalização → use `bash_execute` com o path absoluto. Não repita a chamada de file_tool com variações de argumento.

---

## Fase 2 — Varredura de gaps

Busque sistematicamente por:

| Tipo | Padrão | Exemplo |
|---|---|---|
| `stub-return` | funções que retornam hardcoded/mock sem lógica real | `return True`, `return []`, `return {}` |
| `sleep-fake` | `time.sleep()` em código de produção (não test) | `sleep(0.1)` em implementação |
| `pass-only` | corpos de classe/método com apenas `pass` | `def handle(self): pass` |
| `TODO-flag` | `TODO`, `FIXME`, `HACK`, `XXX` em código não-test | `# TODO: implement` |
| `zero-callers` | funções/métodos/classes definidas mas nunca chamadas | dead code |
| `dead-enum` | valores de Enum definidos mas nunca referenciados | `Status.PENDING` sem uso |
| `dead-config` | chaves de config YAML/JSON lidas mas nunca consumidas | `config['timeout']` sem uso downstream |
| `trivial-flag` | feature flags que nunca mudam de False | `ENABLE_X = False` hardcoded |
| `naming-drift` | símbolo importado/referenciado por nome diferente do definido | alias que disfarça dead link |
| `wiring-gap` | componente criado mas não registrado/conectado | tool instanciada mas não registrada |

Use `bash_execute` com grep para varrer eficientemente:
```bash
bash_execute(command="grep -rn 'TODO\|FIXME\|HACK\|XXX' . --include='*.py' | grep -v test | head -30")
bash_execute(command="grep -rn 'pass$\|return \[\]\|return {}\|return True\|return None' . --include='*.py' | grep -v test | head -30")
```

---

## Fase 3 — Seleção e priorização

Selecione até **5 gaps** para reportar, priorizando por impacto:

1. **CRÍTICO**: wiring-gap (componente inoperante), sleep-fake em produção, stub-return em caminho crítico
2. **ALTO**: zero-callers em código de negócio, dead-enum com efeito semântico
3. **MÉDIO**: TODO-flag em funcionalidade prometida, dead-config
4. **BAIXO**: naming-drift, trivial-flag

Agrupe gaps que tocam nos mesmos arquivos/módulos em UMA issue. Não crie issues separadas para mudanças no mesmo módulo.

---

## Fase 4 — Devil's Advocate (OBRIGATÓRIO para cada gap selecionado)

Para cada gap ANTES de criar a issue, execute o devil's advocate explicitamente:

```
Gap: [descrição]
Argumento pró: [por que vale a pena corrigir]
Contra-argumento: [razão legítima para o gap existir — pode ser intencional?]
Resposta: [por que o contra-argumento não invalida a issue]
Decisão: [ABRIR / DESCARTAR]
```

Se o contra-argumento for mais forte que o argumento pró → **DESCARTAR** (não abrir issue).

---

## Fase 5 — Preparação das issues

Para cada gap selecionado (pós-devil's-advocate aprovado):

1. Leia o template mais adequado com `read_file(path=".github/ISSUE_TEMPLATE/<template>.md")`
2. Preencha TODOS os campos do template
3. Verifique se uma issue similar já existe: `bash_execute(command="gh issue list --state open --limit 50 --json number,title | head -30")`
4. Se issue similar existe → incremente aquela (adicione comentário) em vez de criar duplicata

---

## Fase 6 — Criação das issues

Para cada issue aprovada:
```bash
bash_execute(command="gh issue create --title '<título conciso <70ch>' --body '<body completo>' --label '<label>'")
```

Se operando no repo-pai (Fase 0): adicione a label do subprojeto.

---

## Fase 7a — Verificação pós-criação (OBRIGATÓRIO)

Após CADA `gh issue create`, execute imediatamente:
```bash
bash_execute(command="gh issue view <N> --json title,body,labels")
```

Confirme: título correto, body não-truncado, labels aplicadas.

---

## Fase 7c — REVISÃO mecanizada obrigatória

Para cada issue criada, registre no relatório final com coluna `Status` usando enum:

| Valor | Significa |
|---|---|
| `criada+revisada` | `gh issue view` executado e dados confirmados ✓ |
| `criada+sem_revisao` | ⚠️ view não foi executado — investigate |
| `falhou` | `gh issue create` retornou erro |
| `ja_existe` | issue similar encontrada — incrementada em vez de criada |

**Esta revisão é obrigatória** — não é opcional e não pode ser pulada. Issues sem revisão devem ser explicitadas com `criada+sem_revisao` no relatório.

---

## Fase 8 — Quality self-check (antes de imprimir o relatório)

Agrupe os gaps encontrados por tipo (ver tabela Fase 2).

**Se `dead-config + dead-enum + naming-drift > 80%` do total de gaps encontrados**, emita o seguinte warning no relatório:

> ⚠️ **VARREDURA PREDOMINANTEMENTE DE SUPERFÍCIE**: Os achados desta execução são majoritariamente dead-config/dead-enum/naming-drift (${X}/${total} = ${pct}%). Isso indica que a varredura não atingiu lógica de comportamento. Considere repetir `/EVOLVE` com escopo mais profundo: `/EVOLVE wiring` (gaps de conexão), `/EVOLVE execution-flow` (fluxo de execução), `/EVOLVE security` (gaps de segurança).

---

## Relatório final

Imprima uma tabela markdown:

| # | Issue | Tipo | Impacto | Status |
|---|---|---|---|---|
| 1 | #N - título | tipo | CRÍTICO/ALTO/MÉDIO/BAIXO | criada+revisada |
| ... | | | | |

Seguido de:
- Sumário de tipos encontrados (contagem por tipo)
- Quality self-check result (Fase 8)
- Scope de execução (standalone / subprojeto / monorepo)
- Paths verificados para templates

---

*Skill para Claude Code — deploy em `~/.claude/commands/EVOLVE.md`*
*Companion: issue #149 em elimarcavalli/deile*
