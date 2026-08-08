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
