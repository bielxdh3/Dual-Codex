# Referencia CLI

## Delegacao

```text
python -m dual_codex.cli --config <config> delegate \
  --request-file <request.json> --result-file <result.json>
python -m dual_codex.cli --config <config> delegate \
  --stdin --result-file <result.json>
```

O launcher equivalente no Windows e `scripts/dual-codex.ps1`. Ele usa o Python
do ambiente virtual ativo, depois `.venv`, depois o Python disponivel, injeta o
`src` local em `PYTHONPATH` e funciona em caminhos com espacos.

Selecao do repositorio, em ordem deterministica:

1. `--repository`, quando informado;
2. `repository` no pedido JSON;
3. nenhum alvo: a delegacao e recusada. O `repository` da configuracao serve
   para `status`, `doctor` e o fluxo legado `run`, mas nao e um fallback
   silencioso para `delegate`.

Opcoes adicionais:

- `--allow-dirty`: permite explicitamente um repositorio sujo;
- `--result-file`: recebe uma escrita atomica do resultado;
- `--config`: seleciona a configuracao e os roles.

O comando imprime transicoes `[1/5]` a `[5/5]`, duracao, repositorio resolvido,
diretorio da execucao e uma linha final `DUAL_CODEX_RESULT` com JSON compacto.
O stdout/stderr do Codex nao e repassado ao usuario; logs de diagnostico sao
sanitizados no diretorio da execucao.

## Schemas

- [delegation-request.schema.json](../schemas/delegation-request.schema.json)
- [delegation-result.schema.json](../schemas/delegation-result.schema.json)
- [delegation-report.schema.json](../schemas/delegation-report.schema.json)

Um pedido `implement` contem `schema_version: 1`, `request_id`, `action`,
`repository` e `task`. `constraints`, `context_files` e
`max_correction_cycles` sao opcionais. Um pedido `correct` tambem exige
`parent_request_id` e uma lista de findings com `title` e `details`.

O resultado pode ter `completed`, `failed`, `invalid_request`,
`executor_unavailable` ou `cancelled`. Ele aponta para o report do executor, o
stderr sanitizado, o estado do Git e o diff preservado.

## Operacoes existentes

```text
dual-codex status [--json]
dual-codex dashboard [--port PORT] [--no-open]
dual-codex doctor
dual-codex run task.md
dual-codex account ...
dual-codex role ...
```

`dual-codex dashboard` serve uma interface local em `127.0.0.1` (porta livre
por padrão) e abre o navegador, salvo com `--no-open`. O painel usa chamadas
estruturadas do App Server por conta e degrada métodos ausentes para
`Unknown`/`Not available`; não há endpoint genérico de shell ou filesystem.
Modelos são carregados de `model/list`, `model = ""` é explicitamente o
default herdado, e alterações de model/reasoning/service tier são persistidas
atomicamente para turnos futuros em `config.toml`. A thread atual nunca é
alterada silenciosamente.

### Live Executor

A aba `EXECUTOR LIVE` observa o Executor real por meio do journal JSONL escrito
pela delegacao/App Server. O dashboard nao inicia um App Server proprio para
essa tela. O journal e escolhido no servidor a partir de `runs_dir`, da conta e
role `executor` e da identidade do repositorio; nenhum caminho de arquivo vindo
do navegador e aceito.

O snapshot e o SSE sao limitados por `live_event_journal_max_records`,
`live_event_journal_max_record_bytes` e `live_event_journal_max_detail_bytes`.
O stream suporta replay inicial, cursor ou `Last-Event-ID`, IDs monotonicamente
crescentes, heartbeats e reconexao com backoff. A UI tambem limita as linhas
mantidas no navegador; `Clear View` nao remove o journal nem altera o servidor.

Eventos passam por sanitizacao antes da persistencia: segredos, caminhos
sensiveis, arquivos de autenticacao e reasoning interno sao removidos ou
redigidos. A UI usa insercao textual, nao HTML executavel, e nao exibe
chain-of-thought. Commands, output, diffs, files e mensagens so aparecem com
evidencia protocolar; nao ha inferencia de leituras de arquivo.

O dashboard continua preso a `127.0.0.1` e valida Host/Origin. GET e SSE sao
somente leitura; nao existe shell interativo, browsing arbitrario de arquivos,
CORS ou listener remoto nessa funcionalidade.

## Terminais nativos persistentes

O backend Windows usa um pequeno host Node com `node-pty` sobre ConPTY. Cada
sessao recebe um `CODEX_HOME`, repositorio, PID e log separados; a comunicacao
de controle usa uma named pipe local e nao abre porta de rede.
O host desativa a integracao opcional `apps` do CLI para que o TUI nao dependa
do MCP `codex_apps` do Desktop.

```text
dual-codex terminal start architect-account --role architect --attach
dual-codex terminal start executor-account --role executor --attach
dual-codex terminal list --json
dual-codex terminal send <session-id> "mensagem de follow-up"
dual-codex terminal attach <session-id>
dual-codex terminal terminate <session-id>
```

O modo `dual-codex terminal attach <session-id> --interactive` usa o ConPTY
registrado, saida live por cursor e encaminhamento raw de teclas. `Ctrl-]`
desanexa sem terminar a sessao; quando a automacao possui o lease de entrada,
o humano fica viewer-only. O attach padrao permanece o snapshot legado.

Para exigir a reutilizacao exata, sem start ou fallback silencioso:

```powershell
dual-codex delegate --request-file request.json --result-file result.json --reuse-existing
```

Essa opcao exige o `executor` nativo Windows registrado e pronto, com a mesma
conta, `CODEX_HOME`, repositorio, role e identidade host/PID/epoch. Ausencia,
stale, busy ou pipe inalcançavel falham fechado. Uma TUI aberta externamente
nao pode ser adotada.

`--attach` mantém a saída do TUI visível no terminal atual. O comando
`delegate` usa a sessão persistente do executor por conta e repositório,
mantendo o contexto para correções subsequentes. A dependência Node é
instalada com `npm install`; os testes Python não exigem uma conta Codex real.
