# Integracao com o Codex App

O Codex App permanece como interface conversacional e autoridade de arquitetura
e revisao. O role `executor` usa preferencialmente `codex app-server --stdio`
com JSON-RPC, `CODEX_HOME` proprio e threads persistentes por repositorio. O
ConPTY/TUI nativo permanece como fallback e diagnostico; `codex exec` fica
somente como compatibilidade do fluxo legado `run` e de executaveis mockados.

## Fluxo diario

1. Abra o Codex App.
2. Abra o projeto alvo.
3. Diga: `Use Dual Codex to implement this task.`
4. O App inspeciona o alvo e prepara um pedido JSON preciso.
5. O App chama `scripts/dual-codex.ps1 delegate` e aguarda a linha final.
6. O App le `result.json`, `executor_report_file`, `git_status` e `diff_file`.
7. O App revisa a implementacao real e apresenta o resultado.
8. Para um finding blocking ou important concreto, o App cria um pedido
   `correct` ligado por `parent_request_id`. Nao ha correcao automatica sem
   essa evidencia.

O App deve consultar `status --json` antes de delegar quando precisar confirmar
role, label, login, repositorio, Git e versao do CLI. Delegacao e recusada se o
executor estiver sem role ou sem login.

## Pedido minimo

```json
{
  "schema_version": 1,
  "request_id": "feature-001",
  "action": "implement",
  "repository": "C:/Projects/Target",
  "task": "# Implementacao\n...",
  "constraints": ["Do not edit unrelated files"],
  "authorization": {"allowed_actions": []},
  "context_files": [],
  "review_findings": [],
  "max_correction_cycles": 0
}
```

Para uma missao explicitamente autorizada, substitua a lista vazia pelas
acoes exatas, por exemplo `local_commit`, `normal_push`, `create_branch`, `draft_pr_create` e
`draft_pr_update`; nao inclua capacidades nao necessarias.

O pedido `correct` reutiliza o texto original da tarefa e inclui, por exemplo:

```json
{
  "schema_version": 1,
  "request_id": "feature-001-correction-1",
  "parent_request_id": "feature-001",
  "action": "correct",
  "repository": "C:/Projects/Target",
  "task": "# Implementacao\n...",
  "review_findings": [
    {"severity": "blocking", "title": "Teste falha", "details": "..."}
  ]
}
```

O App nunca deve chamar novamente seu proprio perfil visivel por `codex exec`,
ler `auth.json`, ou afirmar sucesso sem ler o diff. Publicacao permanece
negada por padrao; quando o pedido inclui autorizacao explicita, somente as
acoes listadas em `authorization.allowed_actions` podem ser solicitadas.
Autorizacao para `normal_push` nao inclui `force_push`, e autorizacao para
Draft PR nao inclui merge, release, tag ou deploy.
`create_branch` e uma autorizacao separada: exige que a branch remota esteja
ausente e nunca atualiza uma branch existente.

Operacoes autenticadas de GitHub nao sao executadas dentro do sandbox do
Executor. O control plane pode encaminhar uma solicitacao tipada ao broker
host-side (`dual-codex publish`), que reutiliza essa mesma allow-list,
verifica o repositorio e aplica o CAS de SHA remoto sem transportar tokens,
headers ou credential-store data para o Executor.

## Dois terminais

Para deixar as duas contas visiveis, abra duas janelas nativas e execute:

```powershell
dual-codex terminal start biel3 --role architect --attach
dual-codex terminal start biel4 --role executor --attach
```

O backend App Server envia a tarefa completa por `turn/start`; nao depende do
composer interativo nem de `[Pasted Content]`. O backend TUI envia mensagens
pelo controle local da sessao do executor. O Windows Terminal externo nao e
anexado diretamente ao handle ConPTY; `--attach` fornece a visibilidade por
streaming no host Dual Codex.
O host passa `--disable apps` para nao depender do MCP `codex_apps` do Desktop.
WSL continua sendo o fallback quando o probe nativo deixar de funcionar.
