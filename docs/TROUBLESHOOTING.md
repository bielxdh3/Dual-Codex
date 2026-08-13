# Troubleshooting

## `executor_unavailable`

Execute `status --json` e confirme que `roles.executor` aponta para uma conta
registrada e que `executor.login` e `OK`. Se necessario, use:

```powershell
dual-codex role assign executor <account>
dual-codex account login <account>
```

O resultado nunca inclui `auth.json` ou seu conteudo.

## Repositorio sujo

Por padrao, `require_clean_git = true` interrompe antes do Codex. Commit/stash
as alteracoes ou use conscientemente `--allow-dirty` para uma execucao cujo
estado atual deve ser preservado.

## Lock ativo

Somente uma delegacao por repositorio e permitida. O lock fica em
`<runs_dir>/.locks` e informa request e PID. Um lock vivo e recusado; um lock
stale so e recuperado quando o PID nao existe. Lock invalido ou inacessivel nao
e apagado automaticamente.

## CLI nao encontrado

Use o launcher do repositorio, que nao depende de `dual-codex` estar no PATH:

```powershell
.\scripts\dual-codex.ps1 --config .\config.toml status --json
```

Se o Python nao for encontrado, ative `.venv` ou instale Python 3.11+.

## Falha durante a execucao

Leia o `run_directory` no resultado. O diretorio preserva o pedido sanitizado,
report, stderr sanitizado, estado do Git e diff; qualquer alteracao parcial do
repositorio permanece visivel. A linha final e nao-zero quando o status nao e
`completed`.

## Attach interativo recusado ou viewer-only

Use `terminal attach <session-id>` para um snapshot seguro. `--interactive`
exige um registro Dual Codex nativo valido e nao cria um segundo Codex;
`Ctrl-]` faz detach sem apagar o registro. Durante `delegate`, o lease de
entrada e exclusivo da automacao, portanto o humano anexado fica watch-only.
Ao iniciar um executor, a janela visivel e criada automaticamente como esse
viewer do mesmo ConPTY; `--headless` e a excecao explicita para fluxos sem TUI.

## `--reuse-existing` falha fechado

Essa opcao exige sessao Windows registrada do `executor`, com host/PID, viewer
e epoch validos, named pipe alcancavel, readiness confirmada, sem turno ou lease
concorrente e com a mesma conta, `CODEX_HOME` e identidade do repositorio.
Uma anexacao humana ociosa libera o lease apos uma pequena janela de inatividade;
composicao real continua protegida, e comandos `/model` e `/reasoning` liberam o
lease quando o prompt ocioso volta. O attach readquire o mesmo lease de forma
atomica quando o usuario volta a digitar. Corrija a sessao registrada ou use o
fluxo normal reuse/start quando iniciar uma nova sessao for aceitavel. Uma TUI
aberta fora do Dual Codex nao pode ser adotada por nao possuir proveniencia
verificavel.

## Relatorio estruturado rejeitado

`commands_run` e telemetria opcional: quando omitido, o wrapper canoniza o
campo para `[]`. `summary`, `files_changed`, `tests` e `remaining_issues` nao
sao preenchidos por heuristica; ausencia, tipo invalido ou teste malformado
continua sendo erro de schema. A identidade em `reuse_provenance` vem do
registro/host/pipe/viewer verificados, nunca do auto-relato do modelo.
