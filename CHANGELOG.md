# Changelog

## Unreleased

- Adicionado `dual-codex dashboard`, painel local somente em loopback para
  contas, roles, backend, modelos, reasoning, service tier/Fast, rate limits,
  uso e threads persistentes. Valores configurados e efetivos aparecem
  separados; telemetria ausente permanece `Unknown`/`Not available`.
- Adicionado `service_tier` opcional à configuração. O valor vazio mantém o
  comportamento anterior e as alterações do painel gravam atomicamente apenas
  `config.toml`, sem ler ou regravar credenciais.

- Corrigido o painel para reconstruir reasoning e service tier/Fast conforme o
  modelo selecionado, incluindo o modelo default herdado e avisos para ajustes
  de valores incompatíveis.
- Adicionado editor de conjunto de roles por conta, com aplicação atômica,
  transferência de roles globais e exibição simultânea de múltiplos roles.

## 0.3.0 - 2026-08-08

- Consolidado o registro generico de contas com atribuicao independente de
  roles, labels de exibicao e migracao segura do formato legado.
- Adicionado backend preferencial `app_server` baseado em `codex app-server
  --stdio`: handshake JSON-RPC, threads persistentes por conta/repositorio,
  `thread/start`/`thread/resume`, `turn/start` e eventos oficiais de ciclo de
  vida. O executor continua isolado por `CODEX_HOME`, sem chaves de API
  herdadas, sem listener de rede e sem aprovacao automatica de escalacao.
- Mantido o backend ConPTY como `native_tui` de fallback/diagnostico; o corpo
  completo da tarefa usa App Server diretamente e os artefatos continuam
  disponiveis para auditoria.

- Adicionado handshake de readiness do TUI: a delegacao aguarda o prompt real
  apos update/loading, recusa trust/setup implicito e registra diagnosticos ao
  atingir o timeout configuravel de 60 segundos.
- Endurecida a idle detection contra sugestoes cosmeticas rotativas, exigindo
  marcadores estaveis do banner/modelo/diretorio; a submissao agora confirma
  `task_started`/`Working` em ate 15 segundos e nunca reenvia em timeout.
- Requests `implement`/`correct` agora usam artefato de tarefa dedicado com
  SHA-256 e mensagem curta de controle no ConPTY; follow-ups longos seguem a
  mesma rota para evitar o limite de paste do composer.
- Mensagens de controle agora sao estritamente de uma linha; o ConPTY escreve
  o texto e envia o Enter (`\r`) como operacoes separadas, sem newline implicito.
- A atividade de rollout agora e escopada ao processo/turno atual: o runtime
  captura baseline por sessao, associa o `Codex session_id` e registra rollouts
  historicos ignorados sem alterar os arquivos JSONL.
- O submit ConPTY agora aguarda explicitamente 100 ms entre a escrita do texto
  e o Enter separado, reproduzindo o timing comprovado no probe nativo.
- O envio de controle agora usa acknowledgement do composer por marcador único
  (`[DC:...]`) antes do Enter, com polling raw, timeout de 5 segundos e
  diagnósticos de entrega; o modo bracketed-paste/chunked permaneceu apenas em
  probes e não foi adotado.
- Corrigida a compatibilidade entre `delegate` e o backend ConPTY persistente:
  argumentos exclusivos de `codex exec` nao sao mais enviados ao terminal, e
  relatorios estruturados do executor sao validados antes da classificacao.
- Ligado o `session_id` deterministico por conta e repositorio ao adapter de
  delegacao terminal, preservando a reutilizacao da sessao persistente.
- Adicionado backend Windows persistente com ConPTY via `node-pty`, named pipe
  local, sessoes independentes, follow-up e comandos `dual-codex terminal`.
- Adicionado `backend = "windows"` por conta e status explicito do backend.
- Mantido o probe TUI real em `workspace-write` como criterio de validacao do
  fallback Windows; WSL permanece como fallback secundario.

- Adicionado `dual-codex delegate` para implementacoes e correcoes sem chamar
  Architect/Reviewer pelo CLI oculto.
- Adicionados requests/results versionados, report sanitizado, progresso,
  captura atomica e lock por repositorio.
- Adicionado `status --json`, launcher local do Windows e orientacoes para o
  Codex App.
- Adicionados testes de subprocesso mockado, paths com espacos, falhas,
  login/role, dirty Git, concorrencia e recuperacao de lock stale.
- Corrigido o schema de `tests` do report de delegacao e adicionada validacao
  de regressao para arrays e entries de testes.
- Propagado explicitamente o sandbox de runtime do executor como
  `workspace-write`, com `--add-dir` limitado ao repositorio-alvo; Architect e
  Reviewer continuam em `read-only`.
- Corrigida a classificacao para rejeitar reports validos sem mudancas,
  bloqueios de permissao, testes `not_run` e falhas semanticas, exigindo
  validacao independente do diff e dos testes.

## 0.2.0

- Adicionado registro de contas autenticadas independente dos roles.
- Adicionados comandos de contas, atribuicao/troca de roles e `dual-codex status`.
- Adicionada migracao segura e idempotente do formato `[architect]`/`[executor]`.
- O runtime agora resolve Architect, Executor e Reviewer por role, com fallback de Reviewer para Architect.
- Adicionados testes de BOM, paths Windows com espacos, migracao e protecao contra exposicao de credenciais.
